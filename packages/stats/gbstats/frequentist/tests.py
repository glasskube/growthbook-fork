from abc import abstractmethod
from dataclasses import asdict
from typing import Optional, List

import numpy as np
from pydantic.dataclasses import dataclass
from scipy.stats import t

from gbstats.messages import (
    BASELINE_VARIATION_ZERO_MESSAGE,
    ZERO_NEGATIVE_VARIANCE_MESSAGE,
    ZERO_SCALED_VARIATION_MESSAGE,
    NO_UNITS_IN_VARIATION_MESSAGE,
)
from gbstats.models.statistics import (
    TestStatistic,
    ScaledImpactStatistic,
    RegressionAdjustedStatistic,
    RegressionAdjustedRatioStatistic,
)
from gbstats.models.tests import BaseABTest, BaseConfig, TestResult, Uplift
from gbstats.utils import variance_of_ratios, isinstance_union
from typing import Literal


# Configs
@dataclass
class FrequentistConfig(BaseConfig):
    alpha: float = 0.05
    test_value: float = 0


@dataclass
class SequentialConfig(FrequentistConfig):
    sequential_tuning_parameter: float = 5000


PValueErrorMessage = Literal[
    "NUMERICAL_PVALUE_NOT_CONVERGED",
    "ALPHA_GREATER_THAN_0.5_FOR_SEQUENTIAL_ONE_SIDED_TEST",
]


# Results
@dataclass
class FrequentistTestResult(TestResult):
    p_value: Optional[float] = None
    p_value_error_message: Optional[PValueErrorMessage] = None


@dataclass
class PValueResult:
    p_value: Optional[float | None] = None
    p_value_error_message: Optional[PValueErrorMessage] = None


def frequentist_diff(mean_a, mean_b, relative, mean_a_unadjusted=None) -> float:
    if not mean_a_unadjusted:
        mean_a_unadjusted = mean_a
    if relative:
        return (mean_b - mean_a) / mean_a_unadjusted
    else:
        return mean_b - mean_a


def frequentist_variance(var_a, mean_a, n_a, var_b, mean_b, n_b, relative) -> float:
    if relative:
        return variance_of_ratios(mean_b, var_b / n_b, mean_a, var_a / n_a, 0)
    else:
        return var_b / n_b + var_a / n_a


def frequentist_variance_relative_cuped(
    stat_a: RegressionAdjustedStatistic, stat_b: RegressionAdjustedStatistic
) -> float:
    den_trt = stat_b.n * stat_a.unadjusted_mean**2
    den_ctrl = stat_a.n * stat_a.unadjusted_mean**2
    if den_trt == 0 or den_ctrl == 0:
        return 0  # avoid division by zero
    theta = stat_a.theta if stat_a.theta else 0
    num_trt = (
        stat_b.post_statistic.variance
        + theta**2 * stat_b.pre_statistic.variance
        - 2 * theta * stat_b.covariance
    )
    v_trt = num_trt / den_trt
    const = -stat_b.post_statistic.mean
    num_a = stat_a.post_statistic.variance * const**2 / (stat_a.post_statistic.mean**2)
    num_b = 2 * theta * stat_a.covariance * const / stat_a.post_statistic.mean
    num_c = theta**2 * stat_a.pre_statistic.variance
    v_ctrl = (num_a + num_b + num_c) / den_ctrl
    return v_trt + v_ctrl


def frequentist_variance_relative_cuped_ratio(
    stat_a: RegressionAdjustedRatioStatistic, stat_b: RegressionAdjustedRatioStatistic
) -> float:
    if stat_a.unadjusted_mean == 0 or stat_a.d_statistic_post.mean == 0:
        return 0  # avoid division by zero
    g_abs = stat_b.mean - stat_a.mean
    g_rel_den = np.abs(stat_a.unadjusted_mean)
    nabla_ctrl_0_num = -(g_rel_den + g_abs) / stat_a.d_statistic_post.mean
    nabla_ctrl_0_den = g_rel_den**2
    nabla_ctrl_0 = nabla_ctrl_0_num / nabla_ctrl_0_den
    nabla_ctrl_1_num = (
        stat_a.m_statistic_post.mean * g_rel_den / stat_a.d_statistic_post.mean**2
        + stat_a.m_statistic_post.mean * g_abs / stat_a.d_statistic_post.mean**2
    )
    nabla_ctrl_1_den = g_rel_den**2
    nabla_ctrl_1 = nabla_ctrl_1_num / nabla_ctrl_1_den
    nabla_a = np.array(
        [
            nabla_ctrl_0,
            nabla_ctrl_1,
            -stat_a.nabla[2] / g_rel_den,
            -stat_a.nabla[3] / g_rel_den,
        ]
    )
    nabla_b = stat_b.nabla / g_rel_den
    return (
        nabla_a.T.dot(stat_a.lambda_matrix).dot(nabla_a) / stat_a.n
        + nabla_b.T.dot(stat_b.lambda_matrix).dot(nabla_b) / stat_b.n
    )


class TTest(BaseABTest):
    def __init__(
        self,
        stat_a: TestStatistic,
        stat_b: TestStatistic,
        config: FrequentistConfig = FrequentistConfig(),
    ):
        """Base class for one- and two-sided T-Tests with unequal variance.
        All values are with respect to relative effects, not absolute effects.
        A result prepared for integration with the stats runner can be
        generated by calling `.compute_result()`

        Args:
            stat_a (Statistic): the "control" or "baseline" statistic
            stat_b (Statistic): the "treatment" or "variation" statistic
        """
        super().__init__(stat_a, stat_b)
        self.alpha = config.alpha
        self.test_value = config.test_value
        self.relative = config.difference_type == "relative"
        self.scaled = config.difference_type == "scaled"
        self.traffic_percentage = config.traffic_percentage
        self.total_users = config.total_users
        self.phase_length_days = config.phase_length_days

    @property
    def variance(self) -> float:
        if (
            isinstance(self.stat_a, RegressionAdjustedStatistic)
            and isinstance(self.stat_b, RegressionAdjustedStatistic)
            and self.relative
        ):
            return frequentist_variance_relative_cuped(self.stat_a, self.stat_b)
        elif (
            isinstance(self.stat_a, RegressionAdjustedRatioStatistic)
            and isinstance(self.stat_b, RegressionAdjustedRatioStatistic)
            and self.relative
        ):
            return frequentist_variance_relative_cuped_ratio(self.stat_a, self.stat_b)
        else:
            return frequentist_variance(
                self.stat_a.variance,
                self.stat_a.unadjusted_mean,
                self.stat_a.n,
                self.stat_b.variance,
                self.stat_b.unadjusted_mean,
                self.stat_b.n,
                self.relative,
            )

    @property
    def point_estimate(self) -> float:
        return frequentist_diff(
            self.stat_a.mean,
            self.stat_b.mean,
            self.relative,
            self.stat_a.unadjusted_mean,
        )

    @property
    def critical_value(self) -> float:
        return (self.point_estimate - self.test_value) / np.sqrt(self.variance)

    @property
    def dof(self) -> float:
        # welch-satterthwaite approx
        return pow(
            self.stat_b.variance / self.stat_b.n + self.stat_a.variance / self.stat_a.n,
            2,
        ) / (
            pow(self.stat_b.variance, 2) / (pow(self.stat_b.n, 2) * (self.stat_b.n - 1))
            + pow(self.stat_a.variance, 2)
            / (pow(self.stat_a.n, 2) * (self.stat_a.n - 1))
        )

    @property
    @abstractmethod
    def p_value(self) -> float | None:
        pass

    @property
    @abstractmethod
    def confidence_interval(self) -> List[float]:
        pass

    def _default_output(
        self,
        error_message: Optional[str] = None,
        p_value_error_message: Optional[PValueErrorMessage] = None,
    ) -> FrequentistTestResult:
        """Return uninformative output when AB test analysis can't be performed
        adequately
        """
        return FrequentistTestResult(
            expected=0,
            ci=[0, 0],
            p_value=1,
            uplift=Uplift(
                dist="normal",
                mean=0,
                stddev=0,
            ),
            error_message=error_message,
            p_value_error_message=p_value_error_message,
        )

    def compute_p_value(self) -> PValueResult:
        return PValueResult(
            p_value=self.p_value,
            p_value_error_message=None,
        )

    @property
    def sequential_one_sided_test(self) -> bool:
        return False

    def compute_result(self) -> FrequentistTestResult:
        """Compute the test statistics and return them
        for the main gbstats runner

        Returns:
            FrequentistTestResult -
                note the values are with respect to percent uplift,
                not absolute differences
        """
        if self.stat_a.mean == 0:
            return self._default_output(BASELINE_VARIATION_ZERO_MESSAGE)
        if self.stat_a.unadjusted_mean == 0:
            return self._default_output(BASELINE_VARIATION_ZERO_MESSAGE)
        if self._has_zero_variance():
            return self._default_output(ZERO_NEGATIVE_VARIANCE_MESSAGE)
        if self.sequential_one_sided_test and self.alpha >= 0.5:
            return self._default_output(
                error_message=None,
                p_value_error_message="ALPHA_GREATER_THAN_0.5_FOR_SEQUENTIAL_ONE_SIDED_TEST",
            )

        p_value_result = self.compute_p_value()

        result = FrequentistTestResult(
            expected=self.point_estimate,
            ci=self.confidence_interval,
            p_value=p_value_result.p_value,
            uplift=Uplift(
                dist="normal",
                mean=self.point_estimate,
                stddev=np.sqrt(self.variance),
            ),
            error_message=None,
            p_value_error_message=p_value_result.p_value_error_message,
        )
        if self.scaled:
            result = self.scale_result(result)
        return result

    def scale_result(self, result: FrequentistTestResult) -> FrequentistTestResult:
        if self.phase_length_days == 0 or self.traffic_percentage == 0:
            return self._default_output(ZERO_SCALED_VARIATION_MESSAGE)
        if isinstance_union(self.stat_a, ScaledImpactStatistic):
            if self.total_users:
                adjustment = self.total_users / (
                    self.traffic_percentage * self.phase_length_days
                )
                return FrequentistTestResult(
                    expected=result.expected * adjustment,
                    ci=[result.ci[0] * adjustment, result.ci[1] * adjustment],
                    p_value=result.p_value,
                    uplift=Uplift(
                        dist=result.uplift.dist,
                        mean=result.uplift.mean * adjustment,
                        stddev=result.uplift.stddev * adjustment,
                    ),
                    error_message=None,
                    p_value_error_message=result.p_value_error_message,
                )
            else:
                return self._default_output(NO_UNITS_IN_VARIATION_MESSAGE)
        else:
            error_str = "For scaled impact the statistic must be of type ProportionStatistic, SampleMeanStatistic, or RegressionAdjustedStatistic."
            return self._default_output(error_str)


def one_sided_confidence_interval(
    point_estimate: float, halfwidth: float, lesser: bool = True
) -> List[float]:
    if lesser:
        return [-np.inf, point_estimate + halfwidth]
    else:
        return [point_estimate - halfwidth, np.inf]


def two_sided_confidence_interval(
    point_estimate: float, halfwidth: float
) -> List[float]:
    return [point_estimate - halfwidth, point_estimate + halfwidth]


class TwoSidedTTest(TTest):
    @property
    def p_value(self) -> float:
        return 2 * (1 - t.cdf(abs(self.critical_value), self.dof))  # type: ignore

    @property
    def confidence_interval(self) -> List[float]:
        halfwidth: float = t.ppf(1 - self.alpha / 2, self.dof) * np.sqrt(self.variance)
        return two_sided_confidence_interval(self.point_estimate, halfwidth)


class OneSidedTreatmentGreaterTTest(TTest):
    @property
    def p_value(self) -> float:
        return 1 - t.cdf(self.critical_value, self.dof)  # type: ignore

    @property
    def confidence_interval(self) -> List[float]:
        halfwidth: float = t.ppf(1 - self.alpha, self.dof) * np.sqrt(self.variance)
        return one_sided_confidence_interval(
            self.point_estimate, halfwidth, lesser=False
        )


class OneSidedTreatmentLesserTTest(TTest):
    @property
    def p_value(self) -> float:
        return t.cdf(self.critical_value, self.dof)  # type: ignore

    @property
    def confidence_interval(self) -> List[float]:
        halfwidth: float = t.ppf(1 - self.alpha, self.dof) * np.sqrt(self.variance)
        return one_sided_confidence_interval(
            self.point_estimate, halfwidth, lesser=True
        )


def sequential_rho(alpha, sequential_tuning_parameter, two_sided=True) -> float:
    # eq 161 in https://arxiv.org/pdf/2103.06476v7.pdf
    alpha_arg = alpha if two_sided else 2 * alpha
    return np.sqrt(
        (-2 * np.log(alpha_arg) + np.log(-2 * np.log(alpha_arg) + 1))
        / sequential_tuning_parameter
    )


def sequential_interval_halfwidth(s2, n, sequential_tuning_parameter, alpha) -> float:
    rho = sequential_rho(alpha, sequential_tuning_parameter, two_sided=True)
    # eq 9 in Waudby-Smith et al. 2023 https://arxiv.org/pdf/2103.06476v7.pdf
    return np.sqrt(s2) * np.sqrt(
        (
            (2 * (n * np.power(rho, 2) + 1))
            * np.log(np.sqrt(n * np.power(rho, 2) + 1) / alpha)
            / (np.power(n * rho, 2))
        )
    )


def sequential_interval_halfwidth_one_sided(
    s2, n, sequential_tuning_parameter, alpha
) -> float:
    rho = sequential_rho(alpha, sequential_tuning_parameter, two_sided=False)
    # eq 134 in https://arxiv.org/pdf/2103.06476v7.pdf
    part_1 = s2
    part_2 = 2 * (n * np.power(rho, 2) + 1) / (np.power(n * rho, 2))
    part_3 = np.log(1 + np.sqrt(n * np.power(rho, 2) + 1) / (2 * alpha))
    return np.sqrt(part_1 * part_2 * part_3)


class SequentialTTest(TTest):
    def __init__(
        self,
        stat_a: TestStatistic,
        stat_b: TestStatistic,
        config: SequentialConfig = SequentialConfig(),
    ):
        config_dict = asdict(config)
        self.sequential_tuning_parameter = config_dict.pop(
            "sequential_tuning_parameter"
        )
        super().__init__(stat_a, stat_b, FrequentistConfig(**config_dict))

    @property
    def n(self) -> float:
        return self.stat_a.n + self.stat_b.n

    @property
    def rho(self) -> float:
        # eq 161 in https://arxiv.org/pdf/2103.06476v7.pdf
        return sequential_rho(
            self.alpha, self.sequential_tuning_parameter, two_sided=True
        )

    @property
    @abstractmethod
    def halfwidth(self) -> float:
        pass


class SequentialTwoSidedTTest(SequentialTTest):
    @property
    def halfwidth(self) -> float:
        s2 = self.variance * self.n
        return sequential_interval_halfwidth(
            s2, self.n, self.sequential_tuning_parameter, self.alpha
        )

    @property
    def confidence_interval(self) -> List[float]:
        return two_sided_confidence_interval(self.point_estimate, self.halfwidth)

    @property
    def p_value(self) -> float:
        # eq 155 in https://arxiv.org/pdf/2103.06476v7.pdf
        # slight reparameterization for this quantity below
        st2 = (
            np.power(self.point_estimate - self.test_value, 2)
            * self.n
            / (self.variance)
        )
        tr2p1 = self.n * np.power(self.rho, 2) + 1
        evalue = np.exp(np.power(self.rho, 2) * st2 / (2 * tr2p1)) / np.sqrt(tr2p1)
        return min(1 / evalue, 1)


class SequentialOneSidedTreatmentLesserTTest(SequentialTTest):
    @property
    def sequential_one_sided_test(self) -> bool:
        return True

    @property
    def lesser(self) -> bool:
        return True

    @property
    def halfwidth(self) -> float:
        s2 = self.variance * self.n
        return sequential_interval_halfwidth_one_sided(
            s2,
            self.n,
            self.sequential_tuning_parameter,
            self.alpha,
        )

    @property
    def confidence_interval(self) -> List[float]:
        return one_sided_confidence_interval(
            self.point_estimate, self.halfwidth, lesser=self.lesser
        )

    @property
    def p_value(self) -> float | None:
        return None

    def compute_p_value(self) -> PValueResult:
        difference_type = (
            "relative" if self.relative else "scaled" if self.scaled else "absolute"
        )
        tol = 1e-6
        max_iters = 100
        min_alpha = 1e-5
        max_alpha = 0.4999
        this_config = SequentialConfig(difference_type=difference_type, alpha=min_alpha)
        this_test = (
            SequentialOneSidedTreatmentLesserTTest
            if self.lesser
            else SequentialOneSidedTreatmentGreaterTTest
        )
        ci_index = 1 if self.lesser else 0
        this_ci_small = this_test(
            self.stat_a, self.stat_b, this_config
        ).confidence_interval
        # smaller alpha => bigger confidence interval;
        if self.lesser:
            if this_ci_small[ci_index] < 0:
                return PValueResult(
                    p_value=min_alpha,
                    p_value_error_message=None,
                )
            this_config.alpha = max_alpha
            # bigger alpha => smaller confidence interval;
            this_ci_big = this_test(
                self.stat_a, self.stat_b, this_config
            ).confidence_interval
            if this_ci_big[ci_index] > 0:
                return PValueResult(
                    p_value=max_alpha,
                    p_value_error_message=None,
                )
        else:
            if this_ci_small[ci_index] > 0:
                return PValueResult(
                    p_value=min_alpha,
                    p_value_error_message=None,
                )
            this_config.alpha = max_alpha
            # bigger alpha => smaller confidence interval;
            this_ci_big = this_test(
                self.stat_a, self.stat_b, this_config
            ).confidence_interval
            if this_ci_big[ci_index] < 0:
                return PValueResult(
                    p_value=max_alpha,
                    p_value_error_message=None,
                )
        iters = 0
        this_alpha = 0.5 * (min_alpha + max_alpha)
        diff = 0
        for _ in range(max_iters):
            this_config.alpha = this_alpha
            this_ci = this_test(
                self.stat_a, self.stat_b, this_config
            ).confidence_interval
            diff = this_ci[ci_index] - 0
            if self.lesser:
                if diff > 0:
                    min_alpha = this_alpha
                else:
                    max_alpha = this_alpha
            else:
                if diff < 0:
                    min_alpha = this_alpha
                else:
                    max_alpha = this_alpha
            this_alpha = 0.5 * (min_alpha + max_alpha)
            if abs(diff) < tol:
                break
        converged = abs(diff) < tol and iters != max_iters
        if converged:
            return PValueResult(
                p_value=this_alpha,
                p_value_error_message=None,
            )
        else:
            return PValueResult(
                p_value=None,
                p_value_error_message="NUMERICAL_PVALUE_NOT_CONVERGED",
            )


class SequentialOneSidedTreatmentGreaterTTest(SequentialOneSidedTreatmentLesserTTest):
    @property
    def lesser(self) -> bool:
        return False
