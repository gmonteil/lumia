#!/usr/bin/env python

from .obsdb import obsdb as obsdb_base
import logging
from numpy import *
from tqdm.autonotebook import tqdm
from .Tools import colorize
import h5py
from datetime import datetime
import os
import subprocess

class obsdb:
    def __init__(self, **kwargs):
        self._db = obsdb_base(**kwargs)
        self.footprints_path = kwargs.get('footprints_path', None)

    def __getattr__(self, item):
        return getattr(self._db, item)

    def setupFootprints(self, path=None, names=None, cache=None):
        self.footprints_path = path if path is not None else self.footprints_path
        if self.footprints_path is None :
            logging.error("Unspecified footprints path")

        self.observations.loc[:, 'footprint'] = self._genFootprintNames(names)
        self._checkFootprints(cache=cache)
        self.setup = True

    def _genFootprintNames(self, fnames=None):
        """
        Deduct the names of the footprint files based on their sitename, sampling height and observation time
        Optionally, a user-specified list (for example following a different pattern) can be speficied here.
        :param fnames: A list of footprint file names.
        :return: A list of footprint file names, or the optional "fnames" argument (if it isn't set to None)
        """
        if fnames is None :
            # Create the footprint theoretical filenames :
            codes = [self.sites.loc[s].code for s in self.observations.site]
            fnames = array(
                ['%s.%im.%s.h5'%(c.lower(), z, t.strftime('%Y-%m')) for (c, z, t) in zip(
                    codes, self.observations.height, self.observations.time
                )]
            )
        fnames = [os.path.join(self.footprints_path, f) for f in fnames]
        return fnames

    def _checkCacheFile(self, filename, cache):
        if cache is None :
            cache = self.footprints_path
        file_in_cache = filename.replace(self.footprints_path, cache)
        if not os.path.exists(file_in_cache):
            if not os.path.exists(filename):
                tqdm.write(colorize('<y>[WARNING] File <p:%s> not found! no footprints will be read from it</y>'%filename))
                file_in_cache = None
            elif cache != self.footprints_path:
                subprocess.check_call(['rsync', filename, file_in_cache])
        self.observations.loc[self.observations.footprint == filename, 'footprint'] = file_in_cache
        return file_in_cache

    def _checkFootprints(self, cache=None):
        footprint_files = unique(self.observations.footprint)

        # Loop over the footprint files (not on the obs, for efficiency)
        for fpf in tqdm(footprint_files, desc='Checking footprints'):

            # 1st, check if the footprint exists, and migrate it to cache if needed:
            fpf = self._checkCacheFile(fpf, cache)

            # Then, look if the file has all the individual obs footprints it's supposed to have
            if fpf is not None :
                fp = h5py.File(fpf, mode='r')

                # Times of the obs that are supposed to be in this file
                times = [x.to_pydatetime() for x in self.observations.loc[self.observations.footprint == fpf, 'time']]

                # Check if a footprint exists, for each time
                fp_exists = array([x.strftime('%Y%m%d%H%M%S') in fp for x in times])

                # Some footprints may exist but be empty, get rid of them
                fp_exists[fp_exists] = [size(fp[x.strftime('%Y%m%d%H%M%S')].keys()) > 0 for x in array(times)[fp_exists]]

                # Store that ...
                self.observations.loc[self.observations.footprint == fpf, 'footprint_exists'] = fp_exists.astype(bool)