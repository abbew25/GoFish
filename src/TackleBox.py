import numpy as np
from findiff import FinDiff
from scipy.integrate import simpson as simps
from scipy.interpolate import splrep, splev
from loguru import logger
from ioutils import (
    CosmoResults,
    InputData,
    derivk_geff,
    fitting_formula_interactingneutrinos,
)
import numpy.typing as npt


# from quadpy import quad
from itertools import combinations_with_replacement


def Set_Bait(
    cosmo: CosmoResults,
    data: InputData,
    BAO_only: bool = False,
    beta_phi_fixed: bool = True,
    geff_fixed: bool = True,
    pre_recon: bool = False,
):
    # Compute the reconstruction factors for each redshift bin. Has shape len(z)
    recon = compute_recon(cosmo, data, pre_recon)

    # Precompute some derivative terms. The derivative of P(k) w.r.t. to alpha_perp/alpha_par
    # only needs doing once and then can be scaled by the ratios of sigma8 values. This works because we
    # ignore the derivatives of Dfactor. Has shape (2, len(k), len(mu))
    derPalpha = compute_deriv_alphas(cosmo, BAO_only=BAO_only)
    derPalpha_BAO_only = compute_deriv_alphas(cosmo, BAO_only=True)

    if not beta_phi_fixed and geff_fixed:
        derPbetaphi = compute_deriv_betaphiamplitude(cosmo)
        return recon, derPalpha, derPalpha_BAO_only, derPbetaphi, []
    elif not geff_fixed and beta_phi_fixed:
        derPgeff = compute_derive_geff(cosmo)
        return recon, derPalpha, derPalpha_BAO_only, [], derPgeff
    elif not beta_phi_fixed and not geff_fixed:
        derPbetaphi = compute_deriv_betaphiamplitude(cosmo)
        derPgeff = compute_derive_geff(cosmo)
        return recon, derPalpha, derPalpha_BAO_only, derPbetaphi, derPgeff
    else:
        return recon, derPalpha, derPalpha_BAO_only, [], []


def compute_recon(cosmo: CosmoResults, data: InputData, pre_recon: bool = False):
    muconst = 0.6
    kconst = 0.14

    nP = np.array([0.2, 0.3, 0.5, 1.0, 2.0, 3.0, 6.0, 10.0])
    r_factor = np.array([1.0, 0.9, 0.8, 0.7, 0.6, 0.55, 0.52, 0.5])
    if pre_recon:
        r_factor = np.ones(len(r_factor))

    r_spline = splrep(
        nP, r_factor
    )  # spline for reconstruction factor and signal to noise ratio
    # this function computes the reconstruction factor using old literature equation.

    recon = np.empty(len(cosmo.z))
    kaiser_vec = data.bias + cosmo.f * muconst**2
    nbar_comb = np.sum(data.nbar * kaiser_vec**2, axis=0)
    for iz in range(len(cosmo.z)):
        nP_comb = nbar_comb[iz] * splev(kconst, cosmo.pk[iz]) / 0.1734
        if nP_comb <= nP[0]:
            recon[iz] = r_factor[0]
        elif nP_comb >= nP[-1]:
            recon[iz] = r_factor[-1]
        else:
            recon[iz] = splev(nP_comb, r_spline)
    return recon


def compute_effective_volume(
    cosmo: CosmoResults,
    data: InputData,
    tracer: int,
    skyarea: float,
    kmin: float,
    kmax: float,
):
    """
    Compute triple integral over volume of ( nP/(1 + nP))^2 dmu r^2 dr k^2 dk - as volume eff. for a given k bin.
    Then add up over each k bin.
    """

    nmu = 100
    muvec = np.linspace(0.0, 1.0, nmu)
    recon = compute_recon(cosmo, data)

    dist_with_z = cosmo.dz_comoving  # r(z) vector for redshift vector

    integral_over_z = np.zeros(len(cosmo.z))  # store integrand over z as array

    for j, z in enumerate(cosmo.z):
        signal_int_muvec = np.zeros(nmu)  # store integrand over mu as array

        kminn = np.maximum(
            2.0 * np.pi / dist_with_z[j], kmin
        )  # largest scale set by r(z)

        ks_wanted = np.linspace(kminn, kmax, 1000)

        for i, mu in enumerate(muvec):
            kaiser_vec = data.bias[tracer][j] + cosmo.f[j] * mu**2  # kaiser factor
            nbar = data.nbar[tracer][j]  # number density
            Dpar = mu**2 * ks_wanted**2 * cosmo.Sigma_par[j] ** 2
            Dperp = (1.0 - mu**2) * ks_wanted**2 * cosmo.Sigma_perp[j] ** 2
            Dfactor = np.exp(
                -(recon[j] ** 2) * (Dpar + Dperp) / 2.0
            )  # damping (nonlinear structure growth, accounts for reconstruction noise)
            pkk = splev(ks_wanted, cosmo.pk[j])  # linear matter power

            Pk = (
                pkk * kaiser_vec**2 * Dfactor**2
            )  # redshift space power spectrum at fixed k, mu - vector with varying z
            nP = nbar * Pk
            signal_atk = (nP / (1.0 + nP)) ** 2  # signal as a function of z
            integral_over_k = simps(signal_atk * (ks_wanted**2), ks_wanted) / simps(
                ks_wanted**2, ks_wanted
            )  # integral over k,
            # normalized by integral over k^2 dk

            signal_int_muvec[i] = integral_over_k  # store integrand over mu

        integral_over_z[j] = simps(
            signal_int_muvec, muvec
        )  # integral over mu with simpson rule

    effective_volume = simps(
        integral_over_z * (dist_with_z**2), dist_with_z
    )  # integral over r(z) with simpson rule
    if len(cosmo.z) < 2:
        effective_volume = integral_over_z * (
            (cosmo.rmax**3) / 3.0 - (cosmo.rmin**3) / 3.0
        )  # for a single redshift bin....
    effective_volume *= skyarea  # angular area in steradians s

    return effective_volume


def CovRenorm(
    cov: npt.NDArray,
    parameter_means: npt.NDArray,
    beta_phi_fixed: bool = True,
    geff_fixed: bool = True,
) -> npt.NDArray:
    """Renormalises a covariance matrix with last 3 entries fsigma8, alpha_perp, alpha_par to last 3 entries
        fsigma8, Da, H. Assumes the input covariance matrix is in exactly this order.

    Parameters
    ----------
    cov: np.ndarray
        A 2D covariance matrix. fs8, da, h are the last three columns/rows
    parameter_means: list
        Contains mean values of fs8, da, h

    Returns
    -------
    cov_renorm: np.ndarray
        The converted covariance matrix for the parameters [b_0*sigma8 ... b_npop*sigma8], fsigma8, Da, H
    """

    # Set up the jacobian of the transformation

    if beta_phi_fixed and geff_fixed:
        jacobian = np.identity(cov.shape[0])
        jacobian[-2, -2] = parameter_means[1]
        jacobian[-1, -1] = -parameter_means[2]
    elif not beta_phi_fixed and not geff_fixed:
        jacobian = np.identity(cov.shape[0])
        jacobian[-4, -4] = parameter_means[1]
        jacobian[-3, -3] = -parameter_means[2]
    else:
        jacobian = np.identity(cov.shape[0])
        jacobian[-3, -3] = parameter_means[1]
        jacobian[-2, -2] = -parameter_means[2]

    # Renormalize covariance from alpha's to DA/H
    cov_renorm = jacobian @ cov @ jacobian.T

    return cov_renorm


def compute_deriv_alphas(cosmo: CosmoResults, BAO_only: bool = False):
    from scipy.interpolate import RegularGridInterpolator

    order = 4
    nmu = 100
    dk = 0.0001
    mu = np.linspace(0.0, 1.0, nmu)

    pkarray = np.empty((2 * order + 1, len(cosmo.k)))
    for i in range(-order, order + 1):
        kinterp = cosmo.k + i * dk
        if BAO_only:
            pkarray[i + order] = splev(kinterp, cosmo.pk[0]) / splev(
                kinterp, cosmo.pksmooth[0]
            )
        else:
            pkarray[i + order] = splev(kinterp, cosmo.pk[0])
    derPk = FinDiff(0, dk, acc=4)(pkarray)[order]
    derPalpha = [
        np.outer(
            derPk * cosmo.k, (mu**2 - 1.0)
        ),  # dP(k')/dalpha_perp = dP/dk' * dk'/dalpha_perp
        -np.outer(
            derPk * cosmo.k, (mu**2)
        ),  # dP(k')/dalpha_par = dP/dk' * dk'/dalpha_par
    ]
    derPalpha_interp = [
        RegularGridInterpolator([cosmo.k, mu], derPalpha[i]) for i in range(2)
    ]

    return derPalpha_interp


def compute_deriv_betaphiamplitude(cosmo: CosmoResults):
    from scipy.interpolate import RegularGridInterpolator

    order = 4  # interpolating power spectrum at multiple different ks to get a precise derivative from findiff
    nmu = 100
    dk = 0.0001
    mu = np.linspace(0.0, 1.0, nmu)

    pkarray = np.empty((2 * order + 1, len(cosmo.k)))
    for i in range(-order, order + 1):
        kinterp = cosmo.k + i * dk

        pkarray[i + order] = splev(kinterp, cosmo.pk[0]) / splev(
            kinterp, cosmo.pksmooth[0]
        )

    derPk = FinDiff(0, dk, acc=4)(pkarray)[order]
    # dk_dbeta = fitting_formula_Baumann19(cosmo.k) / cosmo.r_d
    dk_dbeta = (
        fitting_formula_interactingneutrinos(cosmo.k, cosmo.log10Geff, cosmo.r_d)
        / cosmo.r_d
    )

    derPbeta_amplitude = np.outer(
        derPk * dk_dbeta, np.ones(len(mu))
    )  # dP(k')/dbeta = dP/dk' * dk'/dbeta , dk'/dbeta = f(k')/r_s
    derPbeta_interp = [RegularGridInterpolator([cosmo.k, mu], derPbeta_amplitude)]
    return derPbeta_interp


def compute_derive_geff(cosmo: CosmoResults):
    from scipy.interpolate import RegularGridInterpolator

    order = 4  # interpolating power spectrum at multiple different ks to get a precise derivative from findiff
    nmu = 100
    dk = 0.0001
    mu = np.linspace(0.0, 1.0, nmu)

    pkarray = np.empty((2 * order + 1, len(cosmo.k)))
    for i in range(-order, order + 1):
        kinterp = cosmo.k + i * dk

        pkarray[i + order] = splev(kinterp, cosmo.pk[0]) / splev(
            kinterp, cosmo.pksmooth[0]
        )

    derPk = FinDiff(0, dk, acc=4)(pkarray)[order]
    dk_dgeff = derivk_geff(cosmo.k, cosmo.log10Geff, cosmo.r_d, cosmo.beta_phi)
    derPgeff = np.outer(
        derPk * dk_dgeff, np.ones(len(mu))
    )  # dP(k')/dgeff = dP/dk' * dk'/dgeff
    derPgeff_interp = [RegularGridInterpolator([cosmo.k, mu], derPgeff)]
    return derPgeff_interp


def Fish(
    cosmo: CosmoResults,
    kmin: float,
    kmax: float,
    data: InputData,
    iz: int,
    recon: npt.NDArray,
    derPalpha: list,
    derPbeta: list,
    derPgeff: list,
    BAO_only: bool = True,
    GoFast: bool = False,
    beta_phi_fixed: bool = True,
    geff_fixed: bool = True,
):
    """Computes the Fisher information on cosmological parameters biases*sigma8, fsigma8, alpha_perp and alpha_par
        for a given redshift bin by integrating a separate function (CastNet) over k and mu.

    Parameters
    ----------
    cosmo: CosmoResults object
        An instance of the CosmoResults class. Holds the power spectra, BAO damping parameters and
        other cosmological parameters such as f and sigma8 as a function of redshift.
    data: InputData object
        An instance of the InputData class. Holds the galaxy bias and number density for each sample
        as a function of redshift
    iz: int
        The index of the redshift bin we are considering. Used to access the correct parts of data,
        cosmo, recon.
    recon: np.ndarray
        An array containing the expected reduction in BAO damping scales at each redshift. Pre-computed
        using the compute_recon function.
    derPalpha: list
        A list containing 2 scipy.interpolate.RegularGridInterpolator instances. Each one holds an object that
        can be called to return the derivative of power spectrum (full or BAO_only) at a particular k or mu
        value. The first element in the list is dP(k')/alpha_perp, the second is dP(k')/dalpha_par
    BAO_only: logical
        If True compute derivatives w.r.t. to alpha_perp and alpha_par using only the BAO feature in the
        power spectra. Otherwise use the full power spectrum and the kaiser factor. The former matches a standard
        BAO analysis, the latter is more akin to a 'full-shape' analysis. Default = True
    GoFast: logical
        If True uses Simpson's rule for the k and mu integration with 400 k-bins and 100 mu-bins, so fast but
        but approximate. Otherwise, use vector-valued quadrature integration. This latter option can be very slow for
        many tracers. Default = False.

    Returns
    -------
    ManyFish: np.ndarray
        The complete Fisher information on the parameters [b_0*sigma8 ... b_npop*sigma8], fsigma8, alpha_perp, alpha_par
        at the redshift corresponding to index iz. Has size (npop + 3)
    """

    npop = np.shape(data.nbar)[0]
    npk = int(npop * (npop + 1) / 2)

    # Uses Simpson's rule or adaptive quadrature to integrate over all k and mu.
    if GoFast:
        # mu and k values for Simpson's rule
        muvec = np.linspace(0.0, 1.0, 100)
        kvec = np.linspace(kmin, kmax, 400)

        # 2D integration
        ManyFish = simps(
            simps(
                CastNet(
                    muvec,
                    kvec,
                    iz,
                    npop,
                    npk,
                    data,
                    cosmo,
                    recon,
                    derPalpha,
                    derPbeta,
                    derPgeff,
                    BAO_only,
                    beta_phi_fixed,
                    geff_fixed,
                ),
                x=muvec,
                axis=-1,
            ),
            x=kvec,
            axis=-1,
        )

    else:
        # raise Exception(
        #     '"GoFast=False" option needs to be updated. Use GoFast=True to use Simpson\'s rule.'
        # )
        msg = '"GoFast=False" option needs to be updated. Use GoFast=True to use Simpson\'s rule.'
        logger.error(msg)
        raise (ValueError)
        # Integral over mu
        # OneFish = lambda *args: quad(
        #     CastNet, 0.0, 1.0, args=args, limit=10000, epsabs=1.0e-6, epsrel=1.0e-6
        # )[0]

        # # Integral over k
        # ManyFish = quad(
        #     OneFish,
        #     kmin,
        #     kmax,
        #     args=(iz, npop, npk, data, cosmo, recon, derPalpha, BAO_only),
        #     limit=1000,
        #     epsabs=1.0e-5,
        #     epsrel=1.0e-5,
        # )[0]

    # Multiply by the necessary prefactors
    ManyFish *= cosmo.volume[iz] / (2.0 * np.pi**2)

    return ManyFish


def CastNet(
    mu: npt.NDArray,
    k: npt.NDArray,
    iz: int,
    npop: int,
    npk: int,
    data: InputData,
    cosmo: CosmoResults,
    recon: npt.NDArray,
    derPalpha: list,
    derPbeta: list,
    derPgeff: list,
    BAO_only: bool,
    beta_phi_fixed: bool = True,
    geff_fixed: bool = True,
):
    """Compute the Fisher matrix for a vector of k and mu at a particular redshift.

    Parameters
    ----------
    mu: np.ndarray
        The particular mu value(s) to consider
    k: np.ndarray
        The particular k value(s) to consider
    iz: int
        The index of the redshift bin we are considering. Used to access the correct parts of data,
        cosmo, recon.
    npop: int
        The number of different galaxy populations to consider. Used to compute all combinations
        necessary for the power spectra.
    npk: int
        The number of different auto and cross power spectra.
        Equivalent to npop*(npop+1)/2, but passed in to avoid recomputing for each k/mu value.
    data: InputData object
        An instance of the InputData class. Holds the galaxy bias and number density for each sample
        as a function of redshift
    cosmo: CosmoResults object
        An instance of the CosmoResults class. Holds the power spectra, BAO damping parameters and
        other cosmological parameters such as f and sigma8 as a function of redshift.
    recon: np.ndarray
        An array containing the expected reduction in BAO damping scales at each redshift. Pre-computed
        using the compute_recon function.
    derPalpha: list
        A list containing 2 scipy.interpolate.RegularGridInterpolator instances. Each one holds an object that
        can be called to return the derivative of power spectrum (full or BAO_only) at a particular k or mu
        value. The first element in the list is dP(k')/alpha_perp, the second is dP(k')/dalpha_par
    BAO_only: logical
        If True compute derivatives w.r.t. to alpha_perp and alpha_par using only the BAO feature in the
        power spectra. Otherwise use the full power spectrum and the kaiser factor. The former matches a standard
        BAO analysis, the latter is more akin to a 'full-shape' analysis.

    Returns
    -------
    Shoal: np.ndarray
        A four-dimensional array containing the Fisher information for each parameter of interest in the same order
        as the derivatives are returned [b_0*sigma8 ... b_npop*sigma8], fsigma8, alpha_perp, alpha_par for each k
        and mu value. Has shape (npop + 3, npop + 3, len(k), len(mu))
    """

    Shoal = np.empty((npop + 3, npop + 3, len(k), len(mu)))
    if not beta_phi_fixed and not geff_fixed:
        Shoal = np.empty((npop + 5, npop + 5, len(k), len(mu)))
    elif (not beta_phi_fixed and geff_fixed) or (beta_phi_fixed and not geff_fixed):
        Shoal = np.empty((npop + 4, npop + 4, len(k), len(mu)))

    # Compute the kaiser factors for each galaxy sample at the redshift as a function of mu
    kaiser = np.tile(data.bias[:, iz], (len(mu), 1)).T + cosmo.f[iz] * mu**2

    # Compute the BAO damping factor parameter after reconstruction at the redshift of interest
    # as a function of k and mu.
    Dpar = np.outer(mu**2, k**2) * cosmo.Sigma_par[iz] ** 2
    Dperp = np.outer(1.0 - mu**2, k**2) * cosmo.Sigma_perp[iz] ** 2
    Dfactor = np.exp(-(recon**2) * (Dpar + Dperp) / 2.0)

    # Use our splines to compute the power spectrum and derivatives at the redshift as a function of k and mu.
    # To save space we only stored the derivatives at redshift 0, but now scale these to the correct redshift
    # using the ratio of sigma8 values, which is okay to do as the power spectra are all linear. Note this scaling
    # is only necessary for full shape fits, the BAO wiggles do not get scaled by the sigma8 ratio.
    pkval = splev(k, cosmo.pk[iz])
    pksmoothval = splev(k, cosmo.pksmooth[iz])
    coords = [[kval, muval] for kval in k for muval in mu]
    derPalphaval, derPbetaval, derPgeffval = [], [], []
    derPbetaval = (
        [derPbeta[0](coords).reshape(len(k), len(mu))] if not beta_phi_fixed else []
    )
    derPgeffval = (
        [derPgeff[0](coords).reshape(len(k), len(mu))] if not geff_fixed else []
    )
    if BAO_only:
        derPalphaval = [derPalpha[i](coords).reshape(len(k), len(mu)) for i in range(2)]

    else:
        derPalphaval = [
            derPalpha[i](coords).reshape(len(k), len(mu))
            * (cosmo.sigma8[iz] / cosmo.sigma8[0]) ** 2
            for i in range(2)
        ]

    # Loop over each k and mu value and compute the Fisher information for the cosmological parameters
    for i, kval in enumerate(k):
        for j, muval in enumerate(mu):
            derP = compute_full_deriv(
                npop,
                npk,
                kaiser[:, j],
                pkval[i],
                pksmoothval[i],
                muval,
                [derPalphaval[0][i, j], derPalphaval[1][i, j]],
                [derPbetaval[0][i, j]] if not beta_phi_fixed else [],
                [derPgeffval[0][i, j]] if not geff_fixed else [],
                cosmo.f[iz],
                cosmo.sigma8[iz],
                BAO_only,
                beta_phi_fixed=beta_phi_fixed,
                geff_fixed=geff_fixed,
            )

            covP, cov_inv = compute_inv_cov(
                npop, npk, kaiser[:, j], pkval[i], data.nbar[:, iz]
            )

            Shoal[:, :, i, j] = kval**2 * (derP @ cov_inv @ derP.T) * Dfactor[j, i] ** 2

    return Shoal


def compute_inv_cov(
    npop: int, npk: int, kaiser: npt.NDArray, pk: float, nbar: npt.NDArray
):
    """Computes the covariance matrix of the auto and cross-power spectra for a given
        k and mu value, as well as its inverse.

    Parameters
    ----------
    npop: int
        The number of different galaxy populations to consider. Used to compute all combinations
        necessary for the power spectra.
    npk: int
        The number of different auto and cross power spectra.
        Equivalent to npop*(npop+1)/2, but passed in to avoid recomputing for each k/mu value.
    kaiser: np.ndarray
        The kaiser factors for each galaxy population at a fixed mu and redshift. Has length npop.
    pk: float
        The power spectrum value at the given k, mu and redshift values.
    nbar: np.ndarray
        The number density in units of Mpc^3/h^3 for each of the npop samples at the current redshift.

    Returns
    -------
    covariance: np.ndarray
        The covariance matrix between the various auto and cross-power spectra at a given k, mu and redshift.
        Includes shot noise and has size (npk, npk).
    cov_inv: np.ndarray
        The inverse of the covariance matrix between the various auto and cross-power spectra at a given k,
        mu and redshift. Includes shot noise and has size (npk, npk).
    """

    covariance = np.empty((npk, npk))

    # Loop over power spectra of different samples P_12
    for ps1, pair1 in enumerate(combinations_with_replacement(range(npop), 2)):
        n1, n2 = pair1
        # Loop over power spectra of different samples P_34
        for ps2, pair2 in enumerate(combinations_with_replacement(range(npop), 2)):
            n3, n4 = pair2
            # Cov(P_12,P_34)
            pk13, pk24 = kaiser[n1] * kaiser[n3] * pk, kaiser[n2] * kaiser[n4] * pk
            pk14, pk23 = kaiser[n1] * kaiser[n4] * pk, kaiser[n2] * kaiser[n3] * pk
            if n1 == n3:
                pk13 += 1.0 / nbar[n1]
            if n1 == n4:
                pk14 += 1.0 / nbar[n1]
            if n2 == n3:
                pk23 += 1.0 / nbar[n2]
            if n2 == n4:
                pk24 += 1.0 / nbar[n2]
            covariance[ps1, ps2] = pk13 * pk24 + pk14 * pk23

    # identity = np.eye(npk)
    cov_inv = np.linalg.inv(covariance)  # dgesv(covariance, identity)[2]

    return covariance, cov_inv


def compute_full_deriv(
    npop: int,
    npk: int,
    kaiser: npt.NDArray,
    pk: float,
    pksmooth: float,
    mu: float,
    derPalpha: list,
    derPbeta: list,
    derPgeff: list,
    f: float,
    sigma8: float,
    BAO_only: bool,
    beta_phi_fixed: bool = True,
    geff_fixed: bool = True,
):
    """Computes the derivatives of the power spectrum as a function of
        biases*sigma8, fsigma8, alpha_perp and alpha_par (in that order)
        at a given k, mu and redshift

    Parameters
    ----------
    npop: int
        The number of different galaxy populations to consider. This is the number of different bias
        parameters we need to take the derivatives with respect to.
    npk: int
        The number of different auto and cross power spectra to take to derivative of.
        Equivalent to npop*(npop+1)/2, but passed in to avoid recomputing for each k/mu value.
    kaiser: np.ndarray
        The kaiser factors for each galaxy population at a fixed mu and redshift. Has length npop.
    pk: float
        The power spectrum value at the given k, mu and redshift values.
    pksmooth: float
        The smoothed power spectrum value at the given k, mu and redshift values.
    mu: float
        The mu value for the current call.
    derPalpha: list
        The precomputed derivatives of dP(k')/dalpha_perp and dP(k')/dalpha_par at the specific
        value of k, mu and redshift. Contains 2 values, the first is the derivative w.r.t. alpha_perp,
        the second is the derivative w.r.t. alpha_par.
    f: float
        The growth rate of structure at the current redshift.
    sigma8: float
        The value of sigma8 at the current redshift.
    BAO_only: logical
        If True compute derivatives w.r.t. to alpha_perp and alpha_par using only the BAO feature in the
        power spectra. Otherwise use the full power spectrum and the kaiser factor. The former matches a standard
        BAO analysis, the latter is more akin to a 'full-shape' analysis.

    Returns
    -------
    derP: np.ndarray
        The derivatives of all the auto and cross power spectra w.r.t. biases*sigma8, fsigma8, alpha_perp and alpha_par.
        A 2D array where the first dimension corresponds to whichever parameter the derivative is w.r.t. in the following
        order [b_0*sigma8 ... b_npop*sigma8], fsigma8, alpha_perp, alpha_par. The second dimension corresponds to the auto
        or cross-power spectrum under consideration in the order P_00 , P_01, ... , P_0npop, P_11, P_1npop, ..., P_npopnpop.
        The power spectrum order matches the covariance matrix order to allow for easy multiplication.
    """

    derP = np.zeros((npop + 3, npk))
    if beta_phi_fixed and geff_fixed:
        derP = np.zeros((npop + 3, npk))
    elif not beta_phi_fixed and not geff_fixed:
        derP = np.zeros((npop + 5, npk))
    else:
        derP = np.zeros((npop + 4, npk))

    # Derivatives of all power spectra w.r.t to the bsigma8 of each population
    for i in range(npop):
        derP[i, int(i * (npop + (1 - i) / 2))] = 2.0 * kaiser[i] * pk / sigma8
        derP[i, int(i * (npop + (1 - i) / 2)) + 1 : int((i + 1) * (npop - i / 2))] = (
            kaiser[i + 1 :] * pk / sigma8
        )
        for j in range(0, i):
            derP[i, i + int(j * (npop - (1 + j) / 2))] = kaiser[j] * pk / sigma8

    # Derivatives of all power spectra w.r.t fsigma8
    derP[npop, :] = [
        (kaiser[i] + kaiser[j]) * mu**2 * pk / sigma8
        for i in range(npop)
        for j in range(i, npop)
    ]

    if not beta_phi_fixed and geff_fixed:
        # Derivative of beta_phi amplitude w.r.t. alpha_perp and alpha_par
        derP[npop + 3, :] = [
            kaiser[i] * kaiser[j] * derPbeta[0] * pksmooth
            for i in range(npop)
            for j in range(i, npop)
        ]

    if not geff_fixed and beta_phi_fixed:
        # Derivative of geff amplitude w.r.t. alpha_perp and alpha_par
        derP[npop + 3, :] = [
            kaiser[i] * kaiser[j] * derPgeff[0] * pksmooth
            for i in range(npop)
            for j in range(i, npop)
        ]
    if not beta_phi_fixed and not geff_fixed:
        # Derivative of beta_phi amplitude w.r.t. alpha_perp and alpha_par
        derP[npop + 3, :] = [
            kaiser[i] * kaiser[j] * derPbeta[0] * pksmooth
            for i in range(npop)
            for j in range(i, npop)
        ]
        # Derivative of geff amplitude w.r.t. alpha_perp and alpha_par
        derP[npop + 4, :] = [
            kaiser[i] * kaiser[j] * derPgeff[0] * pksmooth
            for i in range(npop)
            for j in range(i, npop)
        ]

    # Derivatives of all power spectra w.r.t the alphas centred on alpha_per = alpha_par = 1.0
    if BAO_only:
        # For BAO_only we only include information on the alpha parameters
        # from the BAO wiggles, and not the Kaiser factor
        derP[npop + 1, :] = [
            kaiser[i] * kaiser[j] * derPalpha[0] * pksmooth
            for i in range(npop)
            for j in range(i, npop)
        ]
        derP[npop + 2, :] = [
            kaiser[i] * kaiser[j] * derPalpha[1] * pksmooth
            for i in range(npop)
            for j in range(i, npop)
        ]

    else:
        # Derivative of mu'**2 w.r.t alpha_perp. Derivative w.r.t. alpha_par is -dmudalpha
        dmudalpha = 2.0 * mu**2 * (1.0 - mu**2)

        # We then just need use to the product rule as we already precomputed dP(k')/dalpha
        derP[npop + 1, :] = [
            (kaiser[i] + kaiser[j]) * f * pk * dmudalpha
            + kaiser[i] * kaiser[j] * derPalpha[0]
            for i in range(npop)
            for j in range(i, npop)
        ]
        derP[npop + 2, :] = [
            -(kaiser[i] + kaiser[j]) * f * pk * dmudalpha
            + kaiser[i] * kaiser[j] * derPalpha[1]
            for i in range(npop)
            for j in range(i, npop)
        ]

    return derP


def shrink_sqr_matrix(sqr_matrix_obj: npt.NDArray, flags: npt.NDArray = np.array([])):
    """
    Function that removed the rows and columns of a square matrix (numpy matrix) if the rows
    and columns that a diagonal element of the matrix coincides with is zero.
    e.g. 1 2 3 4
         2 1 9 0   ----- >     1 2 4
         4 5 0 9               2 1 0
         4 3 2 1               4 3 1

    The third row and column has been removed since M_(2, 2) <= 1e-7
    """
    a = 0
    new_obj = sqr_matrix_obj.copy()

    if len(flags) >= 1:
        new_obj = np.delete(new_obj, flags, 0)
        new_obj = np.delete(new_obj, flags, 1)

    else:
        for i in (np.arange(sqr_matrix_obj.shape[0]))[::-1]:
            if abs(sqr_matrix_obj[i][i]) <= 1e-13:
                a = i
                new_obj = np.delete(new_obj, a, 0)
                new_obj = np.delete(new_obj, a, 1)

    return new_obj
