#!/usr/bin/env python

from datetime import datetime
import lumia
from lumia.interfaces import Interface
from lumia.interfaces.footprint_monthlytot import coarsenTime
from lumia.precon import preconditioner_ray as precon
from lumia.formatters import lagrange
from lumia.Uncertainties import *
from lumia.control import monthlyFlux
from numpy import zeros_like, inner, nan_to_num, sqrt
from lumia.minimizers.congrad import CommFile 
from tqdm import tqdm
from copy import deepcopy

def VecToStruct(vector, interface):
    i_state = 0
    fine_data = deepcopy(interface.ancilliary_data)
    for cat in [x for x in interface.categories if x.optimize]:
        coarse_data = coarsenTime(fine_data[cat.name], cat.optimization_interval, compute_std=False)
        for i_time in range(len(coarse_data['time_interval']['time_start'])):
            for i_lat in range(coarse_data['emis'].shape[1]):
                for i_lon in range(coarse_data['emis'].shape[2]):
                    coarse_data['emis'][i_time, i_lat, i_lon] = vector[i_state]
                    i_state += 1
    return coarse_data['emis'] 

# Parameters
rcfile = 'results/GMD/SRefG/transport.apos.rc'

#################################################
rcf = lumia.rc(rcfile)
start = datetime(*rcf.get('time.start'))
end = datetime(*rcf.get('time.end'))

# Load the pre-processed emissions:
categories = dict.fromkeys(rcf.get('emissions.categories') + rcf.get('emissions.categories.extras', default=[]))
for cat in categories :
    categories[cat] = rcf.get(f'emissions.{cat}.origin')
emis = lagrange.ReadArchive(rcf.get('emissions.prefix'), start, end, categories=categories)

# Initialize the obs operator (transport model)
model = lumia.transport(rcf, formatter=lagrange)

# Initialize the data container (control)
ctrl = monthlyFlux.Control(rcf, preconditioner=precon)

# Create the "Interface" (to convert between control vector and model driver structure)
interface = Interface(ctrl.name, model.name, rcf, ancilliary=emis)

ctrl.setupPrior(interface.StructToVec(emis))#, lsm_from_file=rcf.get('emissions.lsm.file')))
errtype = rcf.get('optim.err.type', default='monthlyPrior')
if errtype == 'monthlyPrior' :
    unc = PercentMonthlyPrior
elif errtype == 'hourlyPrior' :
    unc = PercentHourlyPrior
elif errtype == 'annualPrior' :
    unc = PercentAnnualPrior
elif errtype == 'true_error' :
    unc = ErrorFromTruth
elif errtype == 'field' :
    unc = ErrorFromField
err = unc(rcf, interface)(emis)

ctrl.setupUncertainties(err)

cf = CommFile(rcf.get('var4d.communication.file'), rcf)

converged_eigvals, converged_eigvecs = cf.read_eigsys()
LE = zeros_like(converged_eigvecs)
for ii in tqdm(range(len(converged_eigvals))):
    LE[:,ii] = ctrl.xc_to_x(converged_eigvecs[:,ii], add_prior=False)
Mat2 = 1./converged_eigvals - 1.
dapri = ctrl.get('prior_uncertainty')
dapos = nan_to_num(sqrt(dapri**2 + inner(LE**2, Mat2)))

eapri = VecToStruct(dapri, interface)
eapos = VecToStruct(dapos, interface)

from h5py import File
with File(f'{rcf.get("path.output")}/errors.h5', 'w') as f :
#    f['lats'] = eapri['lats']
#    f['lons'] = eapri['lons']
#    f['start'] = [x.timetuple()[:6] for x in eapri['time_interval']['time_start']]
#    f['end'] = [x.timetuple()[:6] for x in eapri['time_interval']['time_end']]
    f['eapri'] = eapri#['emis']
    f['eapos'] = eapos#['emis']
