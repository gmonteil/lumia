#!/usr/bin/env python

import sys
from argparse import ArgumentParser, REMAINDER
from lumia.obsdb import obsdb
from lumia.formatters.lagrange import ReadStruct as readFluxes
from lumia.formatters.lagrange import CreateStruct, WriteStruct
from numpy import unique, array
from pandas import isnull, read_json
from tqdm import tqdm
from lumia.Tools import logging_tools
import os
from datetime import datetime
import h5py
from lumia.Tools.time_tools import tinterv, time_interval
from lumia.Tools import rctools
from lumia.Tools import Region
import operator

name = 'lagrange'
import logging
logger = logging.getLogger(__name__)

class Footprint:
    def __init__(self, fpfile, verbosity=0, path='', open=True):
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
        if categories is None: categories = emis['cat_list']
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

def forward(db, emis):
    batch = os.environ['INTERACTIVE'] == 'F'
    categories = emis['cat_list']
    dy = {c:[] for c in categories}
    dy['tot'] = []
    dy['id'] = []
    dy['model'] = []
    msg = "Forward"
    nsites = len(unique(db.observations.footprint))
    for fpfile in tqdm(unique(db.observations.loc[-isnull(db.observations.footprint), 'footprint']), total=nsites, desc=msg, disable=batch, leave=True):
        fp = Footprint(fpfile)
        msg = "Forward run (%s)"%fpfile
        nobs = sum(db.observations.footprint == fpfile)
        for obs in tqdm(db.observations.loc[db.observations.footprint == fpfile, :].itertuples(), desc=msg, leave=False, total=nobs, disable=batch):
            dym, tot = fp.applyEmis(obs.time, emis)
            if dym is not None :
                for cat in categories :
                    dy[cat].append(dym.get(cat))
                dy['tot'].append(tot)
                dy['id'].append(obs.Index)
                dy['model'].append(dym)
        fp.close()
    db.observations.loc[:, 'foreground'] = 0.
    for cat in categories :
        db.observations.loc[dy['id'], cat] = dy[cat]
        db.observations.loc[dy['id'], 'foreground'] += array(dy[cat])
    db.observations.loc[dy['id'], 'totals'] = dy['tot']
    db.observations.loc[dy['id'], 'id'] = dy['id']
    db.observations.loc[dy['id'], 'model'] = dy['model']
    return db

def adjoint(adj, db):
    batch = os.environ['INTERACTIVE'] == 'F'
    categories = adj['cat_list']
    msg = 'Adjoint'
    nsites = len(unique(db.footprint))
    db = db.loc[~isnull(db.footprint) & ~isnull(db.dy)]
    for fpfile in tqdm(unique(db.footprint), total=nsites, desc=msg, disable=batch, leave=True):
        fp = Footprint(fpfile)
        msg = "Adjoint run (%s)"%fpfile
        for obs in tqdm(db.loc[db.footprint == fpfile, :].itertuples(), desc=msg, leave=False, disable=batch):
            adj = fp.applyAdjoint(obs.time, obs.dy, adj, categories)
        fp.close()
    return adj


def readArgs(args):
    p = ArgumentParser()
    p.add_argument('--forward', '-f', action='store_true', default=False, help="Do a forward run")
    p.add_argument('--adjoint', '-a', action='store_true', default=False, help="Do an adjoint run")
    p.add_argument('--rc')
    p.add_argument('--db', required=True)
    p.add_argument('--emis', required=True)
    p.add_argument('args', nargs=REMAINDER)
    args = p.parse_args(args)
    return args


if __name__ == "__main__" :

    # Read arguments:
    args = readArgs(sys.argv[1:])

    # Load observations/departures
    db = obsdb(args.db)

    if args.forward :
        # Load emissions
        emis = readFluxes(args.emis)
        # Forward transport :
        db = forward(db, emis)
        db.save(args.db)

    if args.adjoint :
        # Create an empty adjoint structure
        rcf = rctools.rc(args.rc)
        region = Region(rcf)
        categories = [c for c in rcf.get('emissions.categories') if rcf.get('emissions.%s.optimize'%c) == 1]
        start = datetime(*rcf.get('time.start'))
        end = datetime(*rcf.get('time.end'))
        dt = time_interval(rcf.get('emissions.*.interval'))
        adj = CreateStruct(categories, region, start, end, dt)
        adj = adjoint(adj, db.observations)
        WriteStruct(adj, args.emis)