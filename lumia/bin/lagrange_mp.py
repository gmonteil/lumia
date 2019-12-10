#!/usr/bin/env python

import os, sys, subprocess, tempfile, operator, h5py, shutil
from lumia.Tools import rctools
from lumia.obsdb import obsdb
from lumia.Tools.logging_tools import colorize
from lumia.formatters.lagrange import ReadStruct, Struct, WriteStruct, CreateStruct
from numpy import unique, array
from lumia import tqdm
from argparse import ArgumentParser, REMAINDER
from datetime import datetime
from lumia.Tools.time_tools import tinterv, time_interval
from lumia.Tools import Region
from lumia.Tools import Categories

import logging
logger = logging.getLogger(__name__)


# Clean(er/ish) disabling of tqdm in batch mode
# If the "INTERACTIVE" environment variable is defined and set to "F", we redefine tqdm with the following dummy function
if os.environ['INTERACTIVE'] == 'F':
    def tqdm(iterable, *args, **kwargs):
        return iterable


class Footprint:
    def __init__(self, fpfile, path='', open=True):
        self.filename = fpfile
        if not os.path.exists(self.filename):
            logging.warning('Footprint file not found: %s'%self.filename)
        self.ds = h5py.File(fpfile, 'r')
        self.varname = None

    def close(self):
        self.ds.close()

    def loadObs(self, time):
        self.varname = time.strftime('%Y%m%d%H%M%S')
        if not self.varname in self.ds.keys() :
            return None
        times = self.ds[self.varname].keys()
        data = {}
        for tt in times :
            t1, t2 = tt.split('_')
            t1 = datetime.strptime(t1, '%Y%m%d%H%M%S')
            t2 = datetime.strptime(t2, '%Y%m%d%H%M%S')
            if t2 < t1 :
            # Some files may contain fields as tmin_tmax, which shouldn't be used (old and wrong!)
                ttint = tinterv(t2, t1)
                data[ttint] = self.ds[self.varname][tt]
        return data

    def applyEmis(self, time, emis, categories=None, scalefac=1.):
        fp = self.loadObs(time)
        if fp is None : return None, None
        if categories is None: categories = emis.keys()
        dym = {}
        fptot = 0.
        for cat in categories :
            times_cat = [tinterv(t1, t2) for (t1, t2) in zip(emis[cat]['time_interval']['time_start'], emis[cat]['time_interval']['time_end'])]
            dym[cat] = 0.
            fptot = 0.
            for tt in sorted(fp, key=operator.attrgetter('start')):
                ilats = fp[tt]['ilats'][:]
                ilons = fp[tt]['ilons'][:]
                try :
                     dyc = (emis[cat]['emis'][times_cat.index(tt), ilats, ilons]*fp[tt]['resp'][:]).sum()*scalefac
                     dym[cat] += dyc
                     fptot += fp[tt]['resp'][:].sum()
                except ValueError :
                    # This may happen if the footprints and the fluxes are not on the same temporal resolution
                    # (and the footprint files have been generated by an idiot, a.k.a me)
                    # Or just the first observations, too close to the start of the emissions ...
                    return None, None
                except IndexError :
                    print(self.filename)
                    print(time)
                    print(tt, ilats, ilons)
                    print(times_cat)
                    print(fp.keys())

        return dym, fptot

    def applyAdjoint(self, time, dy, adjEmis, cats, scalefac=1.):
        fp = self.loadObs(time)
        if fp is None : return adjEmis
        for cat in cats :
            times_cat = [tinterv(t1, t2) for (t1, t2) in zip(adjEmis[cat]['time_interval']['time_start'], adjEmis[cat]['time_interval']['time_end'])]
            for tt in sorted(fp, key=operator.attrgetter('end')):
                ilats = fp[tt]['ilats'][:]
                ilons = fp[tt]['ilons'][:]
                try :
                    adjEmis[cat]['emis'][times_cat.index(tt), ilats, ilons] += fp[tt]['resp'][:]*dy*scalefac
                except ValueError :
                    return adjEmis
        return adjEmis

class Lagrange:
    def __init__(self, rcf, obs, emfile, mp=False, checkfile=None):
        self.rcf = rctools.rc(rcf)
        self.obs = obsdb(obs)
        self.obsfile = obs
        self.rcfile = rcf
        self.emfile = emfile
        self.executable = __file__
        self.batch = os.environ['INTERACTIVE'] == 'F'
        self.categories = Categories(self.rcf)
        self.checkfile=checkfile
        logger.debug(checkfile)
        if mp :
            self.parallel = True
            self.runForward = self.runForward_mp
            self.runAdjoint = self.runAdjoint_mp
        else :
            self.parallel = False
            self.runForward = self.runForward_sp
            self.runAdjoint = self.runAdjoint_sp

    def runForward_sp(self):
        # Read the emissions:
        emis = ReadStruct(self.emfile)

        # Create containers
        dy = {}
        dy['tot'] = []
        dy['id'] = []
        dy['model'] = []
        for cat in self.categories.list :
            dy[cat] = []

        # Loop over the footprint files
        nsites = len(unique(self.obs.observations.footprint))
        msg = 'Forward run'
        for fpfile in tqdm(unique(self.obs.observations.footprint), total=nsites, desc=msg, disable=self.batch, leave=False):
            fp = Footprint(fpfile)

            # Loop over the obs in the file
            msg = "Forward run (%s)"%fpfile
            nobs = sum(self.obs.observations.footprint == fpfile)
            for obs in tqdm(self.obs.observations.loc[self.obs.observations.footprint == fpfile, :].itertuples(), desc=msg, leave=False, total=nobs, disable=self.batch):
                dym, tot = fp.applyEmis(obs.time, emis)
                if dym is not None :
                    for cat in self.categories.list :
                        dy[cat].append(dym.get(cat))
                    dy['tot'].append(tot)
                    dy['id'].append(obs.Index)
                    dy['model'].append(dym)
            fp.close()

        self.obs.observations.loc[dy['id'], 'id'] = dy['id']
        self.obs.observations.loc[dy['id'], 'totals'] = dy['tot']
        self.obs.observations.loc[dy['id'], 'model'] = dy['model']
        self.obs.observations.loc[:, 'foreground'] = 0.
        for cat in self.categories.list :
            self.obs.observations.loc[dy['id'], cat] = dy[cat]
            self.obs.observations.loc[dy['id'], 'foreground'] += array(dy[cat])
        
        # Write db:
        self.obs.save(self.obsfile)

    def runForward_mp(self):
        files = self.RunParallel('--forward')

        # Retrieve the data
        for dbf in files :
            db = obsdb(dbf)
            self.obs.observations.loc[db.observations.index, 'foreground'] = db.observations.loc[:, 'foreground']
            self.obs.observations.loc[db.observations.index, 'totals'] = db.observations.loc[:, 'totals']
            self.obs.observations.loc[db.observations.index, 'model'] = db.observations.loc[:, 'model']
            for cat in self.categories.list:
                self.obs.observations.loc[db.observations.index, cat] = db.observations.loc[:, cat]
            os.remove(dbf)
        self.obs.save(self.obsfile)

    def runAdjoint_sp(self):
        # Create an empty adjoint structure:
        region = Region(self.rcf)
        categories = [c for c in self.rcf.get('emissions.categories') if self.rcf.get('emissions.%s.optimize'%c) == 1]
        start = datetime(*self.rcf.get('time.start'))
        end = datetime(*self.rcf.get('time.end'))
        dt = time_interval(self.rcf.get('emissions.*.interval'))
        adj = CreateStruct(categories, region, start, end, dt)

        # Loop over the footprint files:
        db = self.obs.observations
        files = unique(db.footprint)
        for fpfile in tqdm(files, total=len(files), desc='Adjoint run', leave=False, disable=self.batch):
            fp = Footprint(fpfile)
            msg = f"Adjoint run {fpfile}"

            # Loop over the obs in the file
            for obs in tqdm(db.loc[db.footprint == fpfile, :].itertuples(), desc=msg, leave=False, disable=self.batch):
                adj = fp.applyAdjoint(obs.time, obs.dy, adj, categories)
            fp.close()

        # Write the adjoint field
        WriteStruct(adj, self.emfile)

    def runAdjoint_mp(self):
        files = self.RunParallel('--adjoint')

        # Retrieve the data
        adj = Struct()
        for adjf in files :
            adj += ReadStruct(adjf)
            os.remove(adjf)

        WriteStruct(adj, self.emfile)

    def RunParallel(self, step):
        pids = []
        files = []
        checkfiles = []
        for idb, dbf in enumerate(self.splitDb()):
            # If it's an adjoint run, we also need a name for the adjoint file:
            if step == '--adjoint':
                emfile = dbf.replace('obs', 'adjoint')
                files.append(emfile)
            else :
                emfile = self.emfile
                files.append(dbf)

            # Launch the transport subprocesses
            cmd = ['python', self.executable, step, '--db', dbf, '--rc', self.rcfile, '--emis', emfile, '--serial']
            if self.checkfile is not None :
                cf = f'{self.checkfile}.{idb}'
                cmd += ['-c', cf]
                checkfiles.append(cf)
            logger.info(colorize(' '.join([x for x in cmd]), 'g'))
            pids.append(subprocess.Popen(cmd, close_fds=True))

        # Let the subprocesses finish
        sigterm = [x.wait() for x in pids]

        # Check success
        self.check_success(checkfiles)

        # Return a list with the file names :
        return files

    def splitDb(self):
        nobs = self.obs.observations.shape[0]
        nchunks = self.rcf.get('model.transport.split', default=1)

        # If we run on just one CPU, don't go further, run on the current file
        if nchunks == 1 :
            yield self.obsfile

        chunk_size = int(nobs/nchunks)
        remain = nobs%nchunks
        if remain != 0 : chunk_size += 1
        for ichunk in range(nchunks):
            db = self.obs.get_iloc(slice(ichunk*chunk_size, (ichunk+1)*chunk_size))

            # Write observation file
            dbfid, dbf = tempfile.mkstemp(dir=self.rcf.get('path.run'), prefix='obs.', suffix='.hdf')

            # The previous function actually creates a file. We need to remove it or we end up with too many open files
            os.fdopen(dbfid).close()

            # Save the file
            db.save(dbf)

            yield dbf

    def check_success(self, files):
        for file in files :
            if file is not None :
                if os.path.exists(file) :
                    os.remove(file)
                else :
                    logger.error("Forward run failed, exiting ...")
                    raise

if __name__ == '__main__':
    logger = logging.getLogger(os.path.basename(__file__))

    # Read arguments:
    p = ArgumentParser()
    p.add_argument('--forward', '-f', action='store_true', default=False, help="Do a forward run")
    p.add_argument('--adjoint', '-a', action='store_true', default=False, help="Do an adjoint run")
    p.add_argument('--serial', '-s', action='store_true', default=False, help="Run on a single CPU")
    p.add_argument('--checkfile', '-c')
    p.add_argument('--rc')
    p.add_argument('--db', required=True)
    p.add_argument('--emis', required=True)
    p.add_argument('args', nargs=REMAINDER)
    args = p.parse_args(sys.argv[1:])

    # Create the transport model
    model = Lagrange(args.rc, args.db, args.emis, mp=not args.serial, checkfile=args.checkfile)

    if args.forward :
        model.runForward()
    if args.adjoint :
        model.runAdjoint()
    if args.checkfile is not None :
        open(args.checkfile, 'w').close()
