#! /usr/bin/env python

# Generic Stage 4 light curve generation pipeline

# Proposed Steps
# -------- -----
# 1.  Read in Stage 3 data products
# 2.  Replace NaNs with zero
# 3.  Determine wavelength bins
# 4.  Increase resolution of spectra (optional)
# 5.  Smooth spectra (optional)
# 6.  Applying 1D drift correction
# 7.  Generate light curves
# 8.  Save Stage 4 data products
# 9.  Produce plots

import os
import time as time_pkg
import numpy as np
import scipy.interpolate as spi
import astraeus.xarrayIO as xrio
from astropy.convolution import Box1DKernel
from . import plots_s4, drift
from ..lib import logedit
from ..lib import readECF
from ..lib import manageevent as me
from ..lib import util
from ..lib import clipping


def genlc(eventlabel, ecf_path=None, s3_meta=None):
    '''Compute photometric flux over specified range of wavelengths.

    Parameters
    ----------
    eventlabel : str
        The unique identifier for these data.
    ecf_path : str; optional
        The absolute or relative path to where ecfs are stored.
        Defaults to None which resolves to './'.
    s3_meta : eureka.lib.readECF.MetaClass
        The metadata object from Eureka!'s S3 step (if running S3 and S4
        sequentially). Defaults to None.

    Returns
    -------
    meta : eureka.lib.readECF.MetaClass
        The metadata object with attributes added by S4.

    Notes
    -----
    History:

    - June 2021 Kevin Stevenson
        Initial version
    - October 2021 Taylor Bell
        Updated to allow for inputs from new S3
    - April 2022 Kevin Stevenson
        Enabled Astraeus
    '''
    # Load Eureka! control file and store values in Event object
    ecffile = 'S4_' + eventlabel + '.ecf'
    meta = readECF.MetaClass(ecf_path, ecffile)
    meta.eventlabel = eventlabel

    if s3_meta is None:
        # Locate the old MetaClass savefile, and load new ECF into
        # that old MetaClass
        s3_meta, meta.inputdir, meta.inputdir_raw = \
            me.findevent(meta, 'S3', allowFail=False)
    else:
        # Running these stages sequentially, so can safely assume
        # the path hasn't changed
        meta.inputdir = s3_meta.outputdir
        meta.inputdir_raw = meta.inputdir[len(meta.topdir):]

    meta = me.mergeevents(meta, s3_meta)

    if not meta.allapers:
        # The user indicated in the ecf that they only want to consider
        # one aperture
        meta.spec_hw_range = [meta.spec_hw, ]
        meta.bg_hw_range = [meta.bg_hw, ]

    # Create directories for Stage 5 outputs
    meta.run_s4 = None
    for spec_hw_val in meta.spec_hw_range:
        for bg_hw_val in meta.bg_hw_range:
            meta.run_s4 = util.makedirectory(meta, 'S4', meta.run_s4,
                                             ap=spec_hw_val, bg=bg_hw_val)

    for spec_hw_val in meta.spec_hw_range:
        for bg_hw_val in meta.bg_hw_range:

            t0 = time_pkg.time()

            meta.spec_hw = spec_hw_val
            meta.bg_hw = bg_hw_val

            # Load in the S3 metadata used for this particular aperture pair
            meta = load_specific_s3_meta_info(meta)

            # Get directory for Stage 4 processing outputs
            meta.outputdir = util.pathdirectory(meta, 'S4', meta.run_s4,
                                                ap=meta.spec_hw, bg=meta.bg_hw)

            # Copy existing S3 log file and resume log
            meta.s4_logname = meta.outputdir + 'S4_' + meta.eventlabel + ".log"
            log = logedit.Logedit(meta.s4_logname, read=meta.s3_logname)
            log.writelog("\nStarting Stage 4: Generate Light Curves\n")
            log.writelog(f"Input directory: {meta.inputdir}")
            log.writelog(f"Output directory: {meta.outputdir}")

            # Copy ecf
            log.writelog('Copying S4 control file', mute=(not meta.verbose))
            meta.copy_ecf()

            log.writelog(f"Loading S3 save file:\n{meta.filename_S3_SpecData}",
                         mute=(not meta.verbose))
            spec = xrio.readXR(meta.filename_S3_SpecData)

            if meta.wave_min is None:
                meta.wave_min = np.min(spec.wave_1d.values)
                log.writelog(f'No value was provided for meta.wave_min, so '
                             f'defaulting to {meta.wave_min}.',
                             mute=(not meta.verbose))
            elif meta.wave_min < np.min(spec.wave_1d.values):
                log.writelog(f'WARNING: The selected meta.wave_min '
                             f'({meta.wave_min}) is smaller than the shortest '
                             f'wavelength ({np.min(spec.wave_1d.values)})')
            if meta.wave_max is None:
                meta.wave_max = np.max(spec.wave_1d.values)
                log.writelog(f'No value was provided for meta.wave_max, so '
                             f'defaulting to {meta.wave_max}.',
                             mute=(not meta.verbose))
            elif meta.wave_max > np.max(spec.wave_1d.values):
                log.writelog(f'WARNING: The selected meta.wave_max '
                             f'({meta.wave_max}) is larger than the longest '
                             f'wavelength ({np.max(spec.wave_1d.values)})')

            meta.n_int, meta.subnx = spec.optspec.shape

            # Determine wavelength bins
            if not hasattr(meta, 'wave_hi'):
                binsize = (meta.wave_max - meta.wave_min)/meta.nspecchan
                meta.wave_low = np.round(np.linspace(meta.wave_min,
                                                     meta.wave_max-binsize,
                                                     meta.nspecchan), 3)
                meta.wave_hi = np.round(np.linspace(meta.wave_min+binsize,
                                                    meta.wave_max,
                                                    meta.nspecchan), 3)
            elif (meta.nspecchan is not None
                  and meta.nspecchan != len(meta.wave_hi)):
                log.writelog(f'WARNING: Your nspecchan value of '
                             f'{meta.nspecchan} differs from the size of '
                             f'wave_hi ({len(meta.wave_hi)}). Using the '
                             f'latter instead.')
                meta.nspecchan = len(meta.wave_hi)
            meta.wave_low = np.array(meta.wave_low)
            meta.wave_hi = np.array(meta.wave_hi)
            meta.wave = (meta.wave_low + meta.wave_hi)/2

            # Define light curve DataArray
            lcdata = xrio.makeLCDA(np.zeros((meta.nspecchan, meta.n_int)),
                                   meta.wave, spec.time.values,
                                   spec.optspec.attrs['flux_units'],
                                   spec.wave_1d.attrs['wave_units'],
                                   spec.optspec.attrs['time_units'],
                                   name='data')
            lcerr = xrio.makeLCDA(np.zeros((meta.nspecchan, meta.n_int)),
                                  meta.wave, spec.time.values,
                                  spec.optspec.attrs['flux_units'],
                                  spec.wave_1d.attrs['wave_units'],
                                  spec.optspec.attrs['time_units'],
                                  name='err')
            lc = xrio.makeDataset({'data': lcdata, 'err': lcerr})
            lc['wave_low'] = (['wavelength'], meta.wave_low)
            lc['wave_hi'] = (['wavelength'], meta.wave_hi)
            lc['wave_mid'] = (lc.wave_hi + lc.wave_low)/2
            lc['wave_err'] = (lc.wave_hi - lc.wave_low)/2
            lc.wave_low.attrs['wave_units'] = spec.wave_1d.attrs['wave_units']
            lc.wave_hi.attrs['wave_units'] = spec.wave_1d.attrs['wave_units']
            lc.wave_mid.attrs['wave_units'] = spec.wave_1d.attrs['wave_units']
            lc.wave_err.attrs['wave_units'] = spec.wave_1d.attrs['wave_units']

            if not hasattr(meta, 'boundary'):
                # The default value before this was added as an option
                meta.boundary = 'extend'

            # FINDME: The current implementation needs improvement,
            # consider using optmask instead of masked arrays
            # Create masked array for steps below
            optspec_ma = np.ma.masked_array(spec.optspec, spec.optmask)
            # Create opterr array with same mask as optspec
            opterr_ma = np.ma.copy(optspec_ma)
            opterr_ma = spec.opterr

            # Do 1D sigma clipping (along time axis) on unbinned spectra
            if meta.sigma_clip:
                log.writelog('Sigma clipping unbinned optimal spectra along '
                             'time axis')
                outliers = 0
                for w in range(meta.subnx):
                    optspec_ma[:, w], nout = \
                        clipping.clip_outliers(optspec_ma[:, w], log,
                                               spec.wave_1d[w], meta.sigma,
                                               meta.box_width, meta.maxiters,
                                               meta.boundary, meta.fill_value,
                                               verbose=meta.verbose)
                    outliers += nout
                # Print summary if not verbose
                log.writelog(f'Identified a total of {outliers} outliers in '
                             f'time series, or an average of '
                             f'{outliers/meta.subnx:.3f} outliers per '
                             f'wavelength',
                             mute=meta.verbose)

            # Apply 1D drift/jitter correction
            if meta.correctDrift:
                # Calculate drift over all frames and non-destructive reads
                # This can take a long time, so always print this message
                log.writelog('Applying drift/jitter correction')
                # Compute drift/jitter
                drift1d, driftmask = drift.spec1D(optspec_ma, meta, log)
                # Replace masked points with moving mean
                drift1d = clipping.replace_moving_mean(
                    drift1d, driftmask, Box1DKernel(meta.box_width))
                lc['drift1d'] = (['time'], drift1d)
                lc['driftmask'] = (['time'], driftmask)
                # Correct for drift/jitter
                for n in range(meta.n_int):
                    # Need to zero-out the weights of masked data
                    weights = (~np.ma.getmaskarray(optspec_ma[n])).astype(int)
                    spline = spi.UnivariateSpline(np.arange(meta.subnx),
                                                  optspec_ma[n], k=3, s=0,
                                                  w=weights)
                    spline2 = spi.UnivariateSpline(np.arange(meta.subnx),
                                                   spec.opterr[n], k=3, s=0,
                                                   w=weights)
                    optspec_ma[n] = spline(np.arange(meta.subnx) +
                                           lc.drift1d[n].values)
                    opterr_ma[n] = spline2(np.arange(meta.subnx) +
                                           lc.drift1d[n].values)
                    # # Merge conflict: Need to test code below
                    # # before implementing
                    # optspec_ma[n] = np.ma.masked_invalid(spline(
                    #     np.arange(meta.subnx)+lc.drift1d[n].values))
                    # opterr_ma[n] = np.ma.masked_invalid(spline2(
                    #     np.arange(meta.subnx)+lc.drift1d[n].values))
                # Plot Drift
                if meta.isplots_S4 >= 1:
                    plots_s4.drift1d(meta, lc)

            # FINDME: optspec mask isn't getting updated when correcting
            # for drift. Also, entire integrations are getting flagged.
            # Need to look into these issues.
            optspec_ma = np.ma.masked_invalid(optspec_ma)
            opterr_ma = np.ma.masked_array(opterr_ma, optspec_ma.mask)
            # spec['optspec_drift']

            # Compute MAD alue
            meta.mad_s4 = util.get_mad(meta, spec.wave_1d.values, optspec_ma,
                                       meta.wave_min, meta.wave_max)
            log.writelog(f"Stage 4 MAD = {str(np.round(meta.mad_s4, 2))} ppm")

            if meta.isplots_S4 >= 1:
                plots_s4.lc_driftcorr(meta, spec.wave_1d, optspec_ma)

            log.writelog("Generating light curves")

            # Loop over spectroscopic channels
            for i in range(meta.nspecchan):
                log.writelog(f"  Bandpass {i} = {lc.wave_low.values[i]:.3f} - "
                             f"{lc.wave_hi.values[i]:.3f}")
                # Compute valid indeces within wavelength range
                index = np.where((spec.wave_1d >= lc.wave_low.values[i]) *
                                 (spec.wave_1d < lc.wave_hi.values[i]))[0]
                # Compute mean flux for each spectroscopic channel
                # Sumation leads to outliers when there are masked points
                lc['data'][i] = np.ma.mean(optspec_ma[:, index], axis=1)
                # Add uncertainties in quadrature
                # then divide by number of good points to get
                # proper uncertainties
                lc['err'][i] = (np.sqrt(np.ma.sum(opterr_ma[:, index]**2,
                                                  axis=1)) /
                                np.ma.MaskedArray.count(opterr_ma))

                # Do 1D sigma clipping (along time axis) on binned spectra
                if meta.sigma_clip:
                    lc['data'][i], outliers = clipping.clip_outliers(
                        lc['data'][i].values, log, lc.wave_mid[i], meta.sigma,
                        meta.box_width, meta.maxiters, meta.boundary,
                        meta.fill_value, verbose=False)
                    log.writelog(f'  Sigma clipped {outliers} outliers in time'
                                 f' series', mute=(not meta.verbose))

                # Plot each spectroscopic light curve
                if meta.isplots_S4 >= 3:
                    plots_s4.binned_lightcurve(meta, lc, i)

            # Calculate total time
            total = (time_pkg.time() - t0) / 60.
            log.writelog('\nTotal time (min): ' + str(np.round(total, 2)))

            log.writelog('Saving results')
            event_ap_bg = (meta.eventlabel + "_ap" + str(spec_hw_val) + '_bg'
                           + str(bg_hw_val))
            # Save Dataset object containing time-series of 1D spectra
            meta.filename_S4_SpecData = (meta.outputdir + 'S4_' + event_ap_bg
                                         + "_SpecData.h5")
            xrio.writeXR(meta.filename_S4_SpecData, spec, verbose=True)
            # Save Dataset object containing binned light curves
            meta.filename_S4_LCData = (meta.outputdir + 'S4_' + event_ap_bg
                                       + "_LCData.h5")
            xrio.writeXR(meta.filename_S4_LCData, lc, verbose=True)

            # Save results
            fname = meta.outputdir+'S4_'+meta.eventlabel+"_Meta_Save"
            me.saveevent(meta, fname, save=[])

            log.closelog()

    return spec, lc, meta


def load_specific_s3_meta_info(meta):
    """Load the specific S3 MetaClass object used to make this aperture pair.

    Parameters
    ----------
    meta : eureka.lib.readECF.MetaClass
        The current metadata object.

    Returns
    -------
    eureka.lib.readECF.MetaClass
        The current metadata object with values from the old MetaClass.
    """
    # Get directory containing S3 outputs for this aperture pair
    inputdir = os.sep.join(meta.inputdir.split(os.sep)[:-2]) + os.sep
    inputdir += f'ap{meta.spec_hw}_bg{meta.bg_hw}'+os.sep
    # Locate the old MetaClass savefile, and load new ECF into
    # that old MetaClass
    meta.inputdir = inputdir
    s3_meta, meta.inputdir, meta.inputdir_raw = \
        me.findevent(meta, 'S3', allowFail=False)
    # Merge S4 meta into old S3 meta
    meta = me.mergeevents(meta, s3_meta)

    return meta
