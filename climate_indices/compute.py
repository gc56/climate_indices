from enum import Enum
import logging

# from dask.array import pad
# from dask_image.ndfilters import convolve
import numba
import numpy as np
import scipy.special
import scipy.stats

from climate_indices import lmoments, utils

# declare the names that should be included in the public API for this module
__all__ = ["Periodicity"]

# ------------------------------------------------------------------------------
# set up a basic, global _logger
logging.basicConfig(
    level=logging.WARN,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d  %H:%M:%S",
)
_logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
class Periodicity(Enum):
    """
    Enumeration type for specifying dataset periodicity.

    'monthly' indicates an array of monthly values, assumed to span full years,
    i.e. the first value corresponds to January of the initial year and any
    missing final months of the final year filled with NaN values,
    with size == # of years * 12

    'daily' indicates an array of full years of daily values with 366 days per year,
    as if each year were a leap year and any missing final months of the final
    year filled with NaN values, with array size == (# years * 366)
    """

    monthly = 12
    daily = 366

    def __str__(self):
        return self.name

    @staticmethod
    def from_string(s):
        try:
            return Periodicity[s]
        except KeyError:
            raise ValueError()


# ------------------------------------------------------------------------------
@numba.jit
def _validate_array(values: np.ndarray,
                    periodicity):
    """

    :param values:
    :param periodicity:
    :return:
    """

    # validate (and possibly reshape) the input array
    if len(values.shape) == 1:

        if periodicity is None:
            message = "1-D input array requires a corresponding periodicity "\
                      "argument, none provided"
            _logger.error(message)
            raise ValueError(message)

        elif periodicity is Periodicity.monthly:
            # we've been passed a 1-D array with shape (months),
            # reshape it to 2-D with shape (years, 12)
            values = utils.reshape_to_2d(values, 12)

        elif periodicity is Periodicity.daily:
            # we've been passed a 1-D array with shape (days),
            # reshape it to 2-D with shape (years, 366)
            values = utils.reshape_to_2d(values, 366)

        else:
            message = "Unsupported periodicity argument: '{0}'".format(periodicity)
            _logger.error(message)
            raise ValueError(message)

    elif (len(values.shape) != 2) or \
            ((values.shape[1] != 12) and (values.shape[1] != 366)):

        # neither a 1-D nor a 2-D array with valid shape was passed in
        message = "Invalid input array with shape: {0}".format(values.shape)
        _logger.error(message)
        raise ValueError(message)

    return values


# ------------------------------------------------------------------------------
@numba.jit
def sum_to_scale(values: np.ndarray,
                 scale: int):
    """
    Compute a sliding sums array using 1-D convolution. The initial
    (scale - 1) elements of the result array will be padded with np.NaN values.
    Missing values are not ignored, i.e. if a np.NaN
    (missing) value is part of the group of values to be summed then the sum
    will be np.NaN

    For example if the first array is [3, 4, 6, 2, 1, 3, 5, 8, 5] and
    the number of values to sum is 3 then the resulting array
    will be [np.NaN, np.NaN, 13, 12, 9, 6, 9, 16, 18].

    More generally:

    Y = f(X, n)

    Y[i] == np.NaN, where i < n
    Y[i] == sum(X[i - n + 1:i + 1]), where i >= n - 1 and X[i - n + 1:i + 1]
        contains no NaN values
    Y[i] == np.NaN, where i >= n - 1 and X[i - n + 1:i + 1] contains
        one or more NaN values

    :param values: the array of values over which we'll compute sliding sums
    :param scale: the number of values for which each sliding summation will
        encompass, for example if this value is 3 then the first two elements of
        the output array will contain the pad value and the third element of the
        output array will contain the sum of the first three elements, and so on
    :return: an array of sliding sums, equal in length to the input values
        array, left padded with NaN values
    """

    # don't bother if the number of values to sum is 1
    if scale == 1:
        return values

    # get the valid sliding summations with 1D convolution
    sliding_sums = np.convolve(values, np.ones(scale), mode="valid")

    # pad the first (n - 1) elements of the array with NaN values
    return np.hstack(([np.NaN] * (scale - 1), sliding_sums))

    # BELOW FOR dask/xarray DataArray integration
    # # pad the values array with (scale - 1) NaNs
    # values = pad(values, pad_width=(scale - 1, 0), mode='constant', constant_values=np.NaN)
    #
    # start = 1
    # end = -(scale - 2)
    # return convolve(values, np.ones(scale), mode='reflect', cval=0.0, origin=0)[start: end]


# ------------------------------------------------------------------------------
@numba.jit
def _pearson3_fitting_values(values):
    """
    This function computes the probability of zero and Pearson Type III
    distribution parameters corresponding to an array of values.

    :param values: 2-D array of values, with each row representing a year
        containing either 12 values corresponding to the calendar months of
        that year, or 366 values corresponding to the days of the year
        (with Feb. 29th being an average of the Feb. 28th and Mar. 1st values for
        non-leap years) and assuming that the first value of the array is
        January of the initial year for an input array of monthly values or
        Jan. 1st of initial year for an input array daily values
    :return: a 2-D array of fitting values for the Pearson Type III
        distribution, with shape (4, 12) for monthly or (4, 366) for daily

        returned_array[0] == probability of zero for each of the calendar time steps
        returned_array[1] == the first Pearson Type III distribution parameter
            for each of the calendar time steps
        returned_array[2] == the second Pearson Type III distribution parameter
            for each of the calendar time steps
        returned_array[3] == the third Pearson Type III distribution parameter
            for each of the calendar time steps
    """

    # validate that the values array has shape: (years, 12) for monthly or (years, 366) for daily
    if len(values.shape) != 2:
        message = "Invalid shape of input data array: {shape}".format(shape=values.shape)
        _logger.error(message)
        raise ValueError(message)

    else:

        time_steps_per_year = values.shape[1]
        if (time_steps_per_year != 12) and (time_steps_per_year != 366):
            message = "Invalid shape of input data array: {shape}".format(shape=values.shape)
            _logger.error(message)
            raise ValueError(message)

    # the values we'll compute and return
    fitting_values = np.zeros((4, time_steps_per_year))

    # compute the probability of zero and Pearson
    # parameters for each calendar time step
    # TODO vectorize the below loop? create a @numba.vectorize() ufunc
    #  for application over the second axis
    for time_step_index in range(time_steps_per_year):

        # get the values for the current calendar time step
        time_step_values = values[:, time_step_index]

        # count the number of zeros and valid (non-missing/non-NaN) values
        number_of_zeros, number_of_non_missing = \
            utils.count_zeros_and_non_missings(time_step_values)

        # make sure we have at least four values that are both non-missing (i.e. non-NaN)
        # and non-zero, otherwise use the entire period of record
        if (number_of_non_missing - number_of_zeros) < 4:

            # we can't proceed, bail out using zeros
            return fitting_values

        # calculate the probability of zero for the calendar time step
        probability_of_zero = 0.0
        if number_of_zeros > 0:

            probability_of_zero = number_of_zeros / number_of_non_missing

        # get the estimated L-moments, if we have
        # more than three non-missing/non-zero values
        if (number_of_non_missing - number_of_zeros) > 3:

            # # remove NaN values from the array, as this invalidates
            # # the calculation within the lmoments fitting function
            # time_step_values = time_step_values[~np.isnan(time_step_values)]

            # get the Pearson Type III parameters for this time
            # step's values within the calibration period
            params = lmoments.fit(time_step_values)
            fitting_values[0, time_step_index] = probability_of_zero
            fitting_values[1, time_step_index] = params["loc"]
            fitting_values[2, time_step_index] = params["scale"]
            fitting_values[3, time_step_index] = params["skew"]

    return fitting_values


# ------------------------------------------------------------------------------
@numba.jit
def _minimum_possible(skew, loc, scale):
    """
    Compute the minimum possible value that can be fitted to a distribution
    described by a set of skew, loc, and scale parameters.

    :param skew:
    :param loc:
    :param scale:
    :return:
    """

    alpha = 4.0 / (skew * skew)

    # calculate the lowest possible value that will
    # fit the distribution (i.e. Z = 0)
    return loc - ((alpha * scale * skew) / 2.0)


# ------------------------------------------------------------------------------
@numba.jit
def _pearson_fit(values: np.ndarray, probabilities_of_zero, skew, loc, scale):
    """
    Perform fitting of an array of value to a Pearson Type III distribution
    as described by the Pearson Type III parameters and probability of zero arguments.

    :param values: an array of values to fit to the Pearson Type III
        distribution described by the skew, loc, and scale
    :param probabilities_of_zero: probability that the value is zero
    :param skew: first Pearson Type III parameter, the skew of the distribution
    :param loc: second Pearson Type III parameter, the loc of the distribution
    :param scale: third Pearson Type III parameter, the scale of the distribution
    """

    # only fit to the distribution if the values array is valid/not missing
    if not np.all(np.isnan(values)):

        minimums_possible = _minimum_possible(skew, loc, scale)
        minimums_mask = values <= minimums_possible
        zero_mask = np.logical_and((values < 0.0005), (probabilities_of_zero > 0.0))
        trace_mask = np.logical_and((values < 0.0005), (probabilities_of_zero <= 0.0))

        # get the Pearson Type III cumulative density function value
        values = scipy.stats.pearson3.cdf(values, skew, loc, scale)

        # turn zero, trace, or minimum values either into either zero
        # or minimum value based on the probability of zero
        values[zero_mask] = 0.0
        values[trace_mask] = 0.0005

        # compute the minimum value possible, and if any values are below
        # that threshold then we set the corresponding CDF to a floor value
        # TODO ask Richard Heim why the use of this floor value, matching
        #  that used for the trace amount?
        nans_mask = np.isnan(values)
        values[np.logical_and(minimums_mask, nans_mask)] = 0.0005

        # account for negative skew
        skew_mask = skew < 0.0
        values[:, skew_mask] = 1 - values[:, skew_mask]

        if not np.all(np.isnan(values)):

            # calculate the probability value, clipped between 0 and 1
            probabilities = np.clip(
                (probabilities_of_zero + ((1.0 - probabilities_of_zero) * values)),
                0.0,
                1.0,
            )

            # the values we'll return are the values at which the probabilities
            # of a normal distribution are less than or equal to the computed
            # probabilities, as determined by the normal distribution's
            # quantile (or inverse cumulative distribution) function
            fitted_values = scipy.stats.norm.ppf(probabilities)

        else:

            fitted_values = values

    else:

        fitted_values = values

    return fitted_values


# ------------------------------------------------------------------------------
@numba.jit
def transform_fitted_pearson(values: np.ndarray,
                             data_start_year: int,
                             calibration_start_year: int,
                             calibration_end_year: int,
                             periodicity):
    """
    Fit values to a Pearson Type III distribution and transform the values
    to corresponding normalized sigmas.

    :param values: 2-D array of values, with each row representing a year containing
                   twelve columns representing the respective calendar months,
                   or 366 columns representing days as if all years were leap years
    :param data_start_year: the initial year of the input values array
    :param calibration_start_year: the initial year to use for the calibration period
    :param calibration_end_year: the final year to use for the calibration period
    :param periodicity: the periodicity of the time series represented by the input
                        data, valid/supported values are 'monthly' and 'daily'
                        'monthly' indicates an array of monthly values, assumed
                        to span full years, i.e. the first value corresponds
                        to January of the initial year and any missing final
                        months of the final year filled with NaN values,
                        with size == # of years * 12
                        'daily' indicates an array of full years of daily values
                        with 366 days per year, as if each year were a leap year
                        and any missing final months of the final year filled
                        with NaN values, with array size == (# years * 366)
    :return: 2-D array of transformed/fitted values, corresponding in size
             and shape of the input array
    :rtype: numpy.ndarray of floats
    """

    # if we're passed all missing values then we can't compute anything,
    # and we'll return the same array of missing values
    if (np.ma.is_masked(values) and values.mask.all()) or np.all(np.isnan(values)):
        return values

    # validate (and possibly reshape) the input array
    values = _validate_array(values, periodicity)

    # determine the end year of the values array
    data_end_year = data_start_year + values.shape[0]

    # make sure that we have data within the full calibration period,
    # otherwise use the full period of record
    if (calibration_start_year < data_start_year) \
            or (calibration_end_year > data_end_year):
        calibration_start_year = data_start_year
        calibration_end_year = data_end_year

    # get the year axis indices corresponding to the calibration start and end years
    calibration_begin_index = calibration_start_year - data_start_year
    calibration_end_index = (calibration_end_year - data_start_year) + 1

    # compute the values we'll use to fit to the Pearson Type III distribution
    pearson_values = \
        _pearson3_fitting_values(values[calibration_begin_index:calibration_end_index, :])

    loc = pearson_values[1]
    scale = pearson_values[2]
    skew = pearson_values[3]
    probability_of_zero = pearson_values[0]

    # fit each value to the Pearson Type III distribution
    values = _pearson_fit(values, probability_of_zero, skew, loc, scale)

    return values


# ------------------------------------------------------------------------------
@numba.jit
def transform_fitted_gamma(values: np.ndarray,
                           data_start_year: int,
                           calibration_start_year: int,
                           calibration_end_year: int,
                           periodicity):
    """
    Fit values to a gamma distribution and transform the values to corresponding
    normalized sigmas.

    :param values: 2-D array of values, with each row typically representing a year
                   containing twelve columns representing the respective calendar
                   months, or 366 days per column as if all years were leap years
    :param data_start_year: the initial year of the input values array
    :param calibration_start_year: the initial year to use for the calibration period
    :param calibration_end_year: the final year to use for the calibration period
    :param periodicity: the type of time series represented by the input data,
        valid values are 'monthly' or 'daily'
        'monthly': array of monthly values, assumed to span full years,
        i.e. the first value corresponds to January of the initial year and any
        missing final months of the final year filled with NaN values, with
        size == # of years * 12
        'daily': array of full years of daily values with 366 days per year,
        as if each year were a leap year and any missing final months of the final
        year filled with NaN values, with array size == (# years * 366)
    :return: 2-D array of transformed/fitted values, corresponding in size
        and shape of the input array
    :rtype: numpy.ndarray of floats
    """

    # if we're passed all missing values then we can't compute anything,
    # then we return the same array of missing values
    if (np.ma.is_masked(values) and values.mask.all()) or np.all(np.isnan(values)):
        return values

    # validate (and possibly reshape) the input array
    values = _validate_array(values, periodicity)

    # find the percentage of zero values for each time step
    zeros = (values == 0).sum(axis=0)
    probabilities_of_zero = zeros / values.shape[0]

    # replace zeros with NaNs
    values[values == 0] = np.NaN

    # determine the end year of the values array
    data_end_year = data_start_year + values.shape[0]

    # make sure that we have data within the full calibration period,
    # otherwise use the full period of record
    if (calibration_start_year < data_start_year) or \
            (calibration_end_year > data_end_year):
        calibration_start_year = data_start_year
        calibration_end_year = data_end_year

    # get the year axis indices corresponding to
    # the calibration start and end years
    calibration_begin_index = calibration_start_year - data_start_year
    calibration_end_index = (calibration_end_year - data_start_year) + 1

    # get the values for the current calendar time step
    # that fall within the calibration years period
    calibration_values = values[calibration_begin_index:calibration_end_index, :]

    # compute the gamma distribution's shape and scale parameters, alpha and beta
    # TODO explain this better
    means = np.nanmean(calibration_values, axis=0)
    log_means = np.log(means)
    logs = np.log(calibration_values)
    mean_logs = np.nanmean(logs, axis=0)
    a = log_means - mean_logs
    alphas = (1 + np.sqrt(1 + 4 * a / 3)) / (4 * a)
    betas = means / alphas

    # find the gamma probability values using the gamma CDF
    gamma_probabilities = scipy.stats.gamma.cdf(values, a=alphas, scale=betas)

    # TODO explain this
    # (normalize including the probability of zero, putting into the range [0..1]?)
    probabilities = probabilities_of_zero + \
                    ((1 - probabilities_of_zero) * gamma_probabilities)

    # the values we'll return are the values at which the probabilities of
    # a normal distribution are less than or equal to the computed probabilities,
    # as determined by the normal distribution's quantile (or inverse
    # cumulative distribution) function
    return scipy.stats.norm.ppf(probabilities)
