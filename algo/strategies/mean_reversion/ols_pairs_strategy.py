import backtrader as bt
import numpy as np
from collections import deque
from statsmodels.regression.linear_model import OLS
from algo.cointegration.augmented_dickey_fuller import adf_stationarity
from algo.cointegration.engle_granger import engle_granger_bidirectional

# Important TODOs:
# [] For now we ignore roll costs.
# [] Smooth the price data? Hourly might be ok.


class OLSPairsTradingStrategy(bt.Strategy):

    def __init__(self):
        print(f"Initial Position:\n {self.position}")

        # Initial run condtions
        self.model_trained = False  # Check if the model has been trained at least once.
        self.count = 0  # Resets
        self.global_count = 0  # Never resets

        # TODO: define in params config.
        # Data Parameters
        use_fixed_train_size = True
        # use_fixed_train_size = False

        # Number of data points needed to train the initial model.
        self.num_train_initial = 24 * 7 * 13  # Weeks.

        # Number of data points to elapse before retraining the model with the original and new data
        self.num_test = 24 * 7  # num_train_ongoing = 24*7

        # Initial OU conditions
        self.alpha = None
        self.beta = None

        # Initial Time Series
        self.X = np.array([])   # Spread
        self.S0 = []            # Asset 0
        self.S1 = []            # Asset 1
        if use_fixed_train_size:
            self.S0 = deque(self.S0, maxlen=self.num_train_initial)
            self.S1 = deque(self.S1, maxlen=self.num_train_initial)

        # Trading Signals
        self.z_entry = 0.5
        self.z_exit = 0.1

        # Tracking.
        self.order_buy = None
        self.order_sell = None

    def next(self):
        self.count += 1
        self.global_count += 1

        # Current prices of asset0 and asset1.
        p0 = self.data0.close[0]
        p1 = self.data1.close[0]

        # Do nothing if at least 1 data stream has missing data.
        if p0 is None or p1 is None:
            print(f"NaN: p0={p0} | p1={p1}.")
            return

        # Store most recent asset prices.
        self.S0.append(p0)
        self.S1.append(p1)

        # Train OU-Model for the first time
        if self.count >= self.num_train_initial and not self.model_trained:
            self.train()

        # Do nothing if there has not been enough data to train the model.
        if not self.model_trained:
            return

        # (Re-)Train OU-Model with updated data as soon as we are not in the market.
        if self.count >= self.num_test \
                and not self.order_buy \
                and not self.order_sell:
            self.train()

        # X will have been populated during training.
        current_spread = self.alpha * p0 - self.beta * p1
        self.X = np.append(self.X, current_spread)

        # Current z_score.
        z_score = (current_spread - np.mean(self.X)) / np.std(self.X)

        # Determine position.
        if z_score <= -self.z_entry:
            self.long_portfolio()

        elif z_score >= self.z_entry:
            self.short_portfolio()

        elif np.abs(z_score) <= self.z_exit:
            self.exit_market()

    def train(self):
        S0 = np.array(self.S0)
        S1 = np.array(self.S1)

        res_yx = OLS(S1, S0).fit().params

        # Update hedge parameters: (alpha, beta) for use until the next re-training.
        self.alpha = 1
        self.beta = res_yx[0]

        # Update purchase parameters: (A, B) for use until the next re-training.
        self.A = self.alpha * self.S0[0]
        self.B = self.beta * self.S1[0]

        # (Re-)Compute historic spread using (new) hedging parameters.
        self.X = self.alpha * S0 - self.beta * S1

        pretrade_checks(S0, S1, self.X)

        # Reset counter for ongoing training.
        self.count = 0

        # Record that the model has been trained at least once.
        self.model_trained = True

    def long_portfolio(self):
        # Do nothing if already in the market.
        if self.order_buy is not None or self.order_sell is not None:
            return

        # Buy asset S0 and sell asset S1.
        print(f"{self.global_count} {self.count} LONG PORTFOLIO")
        self.order_buy = self.buy(data=self.data0, size=self.A, exectype=bt.Order.Market)
        self.order_sell = self.sell(data=self.data1, size=self.B, exectype=bt.Order.Market)

    def short_portfolio(self):
        # Do nothing if already in the market.
        if self.order_buy is not None or self.order_sell is not None:
            return

        # Sell asset S0 and buy asset S1.
        print(f"{self.global_count} {self.count} SHORT PORTFOLIO")
        self.order_sell = self.sell(data=self.data0, size=self.A, exectype=bt.Order.Market)
        self.order_buy = self.buy(data=self.data1, size=self.B, exectype=bt.Order.Market)

    def exit_market(self):
        # Do nothing if already out of the market.
        if self.order_buy is None and self.order_sell is None:
            return

        print(f"{self.global_count} {self.count} EXITING MARKET")
        self.close(data=self.data0, exectype=bt.Order.Market)
        self.close(data=self.data1, exectype=bt.Order.Market)

    def log(self, message: str, date_time=None):
        date_time = date_time or self.data0.datetime[0]
        date_time = bt.num2date(date_time)
        print(f"{date_time.isoformat()}", message)
        # https://www.backtrader.com/docu/quickstart/quickstart/#do-not-only-buy-but-sell

    def notify_order(self, order):
        if order.status in [bt.Order.Submitted, bt.Order.Accepted]:
            # Wait for further notifications.
            return

        if order.status == order.Completed:
            if order.isbuy():
                msg = f"BUY COMPLETE: {order.executed.price}"
                self.log(msg, order.executed.dt)
                # Allow new orders.
                self.order_buy = None

            else:
                msg = f"SELL COMPLETE: {order.executed.price}"
                self.log(msg, order.executed.dt)
                # Allow new orders.
                self.order_sell = None

        elif order.status in [order.Expired, order.Canceled, order.Margin]:
            self.log('%s ,' % order.Status[order.status])


def pretrade_checks(S0, S1, spread) -> None:
    results = {
        # "adf_S0_non-stat_c": not adf_stationarity(S0, trend="c"),
        # "adf_S1_non-stat_c": not adf_stationarity(S1, trend="c"),
        # Test for stationarity in the actual spread series generated by the OU Model/
        "adf_c": adf_stationarity(spread, trend="c"),
        # Test for cointegration in the underlying asset price series.
        "engle_granger_c": engle_granger_bidirectional(S0, S1, trend="c"),
    }

    print(results)
