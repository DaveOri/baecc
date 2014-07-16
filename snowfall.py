"""Tools for estimating density and other properties of falling snow"""
import numpy as np
import pandas as pd
import read
from scipy.optimize import minimize
import matplotlib.pyplot as plt
import copy

TAU = 2*np.pi
RHO_W = 1000

class Method1(read.PrecipMeasurer):
    """Calculate snowfall rate from particle size and velocity data."""
    def __init__(self, dsd, pipv, pluvio, unbias=False, autoshift=False,
                 quess=(0.01, 2.1), bnd=((0, 0.1), (1, 3)), rule='15min'):
        self.dsd = dsd
        self.pipv = pipv
        self.pluvio = pluvio
        self.quess = quess
        self.bnd = bnd
        self.rule = rule
        self.result = None
        self.ab = None
        self.scale = 1e6 # mg --> kg
        if autoshift:
            self.autoshift()
        if unbias:
            self.noprecip_bias()
            
    def __repr__(self):
        return """%s sampling
                  consts: %s""" % (self.rule, self.ab)
        
    @classmethod
    def from_hdf(cls, dt_start, dt_end, filenames=['../DATA/baecc.h5'], **kwargs):
        """Create Method1 object from a hdf file."""
        for dt in [dt_start, dt_end]:
            dt = pd.datetools.to_datetime(dt)
        pluvio200 = read.Pluvio(filenames, hdf_table='pluvio200')
        pluvio400 = read.Pluvio(filenames, hdf_table='pluvio400')
        dsd = read.PipDSD(filenames, hdf_table='pip_dsd')
        pipv = read.PipV(filenames, hdf_table='pip_vel')
        for instr in [pluvio200, pluvio400, dsd, pipv]:
            instr.set_span(dt_start, dt_end)
        m200 = cls(dsd, pipv, pluvio200, **kwargs)
        m400 = cls(dsd, pipv, pluvio400, **kwargs)
        return m200, m400
        
    def between_datetime(self, dt_start, dt_end, inplace=False):
        """Select data only in chosen time frame."""
        for dt in [dt_start, dt_end]:
            dt = pd.datetools.to_datetime(dt)
        if inplace:
            m = self
        else:
            m = copy.deepcopy(self)
        for instr in [m.dsd, m.pipv, m.pluvio]:
            instr.between_datetime(dt_start, dt_end, inplace=True)
        m.pluvio.bias = 0
        return m
        
    def rainrate(self, consts=None, simple=False):
        """Calculate rainrate using given or saved constants."""
        if self.ab is not None and consts is None:
            consts = self.ab
        if simple:
            r = self.sum_over_d(self.r_rho, rho=consts[0])
        else:
            r = self.sum_over_d(self.r_ab, alpha=consts[0], beta=consts[1])
        return r.reindex(self.pluvio.rainrate(rule=self.rule).index).fillna(0)
        
    def n(self, d):
        """Number concentration N(D) 1/(mm*m**3)"""
        return self.dsd.corrected_data()[str(d)].resample(self.rule, how=np.mean, closed='right', label='right')
        
    def sum_over_d(self, func, **kwargs):
        dD = self.dsd.d_bin
        result = self.df_zeros()
        for d in self.dsd.bin_cen():
            result = result.add(func(d, **kwargs)*dD, fill_value=0)
        return result
        
    def r_ab(self, d, alpha, beta):
        """(mm/h)/(m/s) / kg/m**3 * mg/mm**beta * m**beta * m/s * 1/(mm*m**3)
        """
        return 3.6/RHO_W*alpha*d**beta*self.v_fall(d)*self.n(d)
        
    def r_rho(self, d, rho):
        """(mm/h)/(m/s) * kg/m**3 * mm**3 * m/s * 1/(mm*m**3)"""
        return 3.6*TAU/12*rho*d**3*self.v_fall(d)*self.n(d)
        
    def v_fall(self, d):
        """v(D) m/s, query is slow"""
        dD = self.dsd.d_bin
        vcond = 'Wad_Dia > %s and Wad_Dia < %s' % (d-0.5*dD, d+0.5*dD)
        vel = self.pipv.data.query(vcond).vel_v
        if vel.empty:
            return self.df_zeros()
        return vel.resample(self.rule, how=np.mean, closed='right', label='right')
        
    def n_t(self):
        """total concentration"""
        return self.sum_over_d(self.n)
        
    def d_m(self):
        """mass weighted mean diameter"""
        d4n = lambda d: d**4*self.n(d)
        d3n = lambda d: d**3*self.n(d)
        return self.sum_over_d(d4n)/self.sum_over_d(d3n)
            
    def df_zeros(self):
        return self.pluvio.acc(self.rule)*0
    
    def noprecip_bias(self, inplace=True):
        """Wrapper to unbias pluvio using LWC calculated from PIP data."""
        return self.pluvio.noprecip_bias(self.pipv.lwc(), inplace=inplace)
        
    def cost(self, c, use_accum=False):
        """Cost function for minimization"""
        pip_acc = self.rainrate(c)
        if use_accum:
            pip_acc = pip_acc.cumsum()
            cost_method = self.pluvio.acc
        else:
            cost_method = self.pluvio.rainrate
        return abs(pip_acc.add(-1*cost_method(self.rule)).sum())
        
    def cost_lsq(self, beta):
        """Single variable cost function using lstsq to find linear coef."""
        alpha = self.alpha_lsq(beta)
        return self.cost([alpha, beta])
    
    def const_lsq(self, c, simple):
        acc_arr = self.rainrate(consts=c, simple=simple).cumsum().values
        A = np.vstack([acc_arr, np.ones(len(acc_arr))]).T
        y = self.pluvio.acc(self.rule).values
        return np.linalg.lstsq(A, y)[0][0]
        
    def alpha_lsq(self, beta):
        """Wrapper for const_lsq to calculate alpha"""
        return self.const_lsq(c=[1, beta], simple=False)
        
    def density_lsq(self):
        """Wrapper for const_lsq to calculate mean particle density"""
        return self.const_lsq(c=[1], simple=True)
        
    def density(self, fltr=True):
        """Calculates mean density estimate for each timeframe."""
        rho_r_pip = self.rainrate(consts=[1], simple=True)
        if fltr:
            rho_r_pip[rho_r_pip < 1000] = np.nan # filter
        return self.pluvio.rainrate(self.rule)/rho_r_pip

    def minimize(self, method='SLSQP', **kwargs):
        """Find constants for calculating particle masses. Save and return results."""
        print('Optimizing constants...')
        self.result = minimize(self.cost, self.quess, method=method, **kwargs)
        self.ab = self.result.x
        return self.result
        
    def minimize_lsq(self):
        """Find beta by minimization and alpha by linear least square."""
        print('Optimizing constants...')
        self.result = minimize(self.cost_lsq, self.quess[1], method='Nelder-Mead')
        #self.result = minimize(self.cost_lsq, self.quess[1], method='SLSQP', bounds=self.bnd[1])
        print(self.result.message)
        beta = self.result.x[0]
        alpha = self.alpha_lsq(beta)
        self.ab = [alpha, beta]
        return self.result
        
    def time_range(self):
        """data time ticks on minute interval"""
        return pd.date_range(self.pluvio.data.index[0], self.pluvio.data.index[-1], freq='1min')
        
    def plot(self, kind='line', **kwargs):
        """Plot calculated (PIP) and pluvio rainrates."""
        if self.ab is None:
            print('Constants not defined. Will now find them via minimization.')
            self.minimize_lsq()
        f, axarr = plt.subplots(4, sharex=True)
        self.intensity().plot(label='PIP', kind=kind, ax=axarr[0], **kwargs)
        self.pluvio.intensity(rule=self.rule).plot(label=self.pluvio.name,
                                    kind=kind, ax=axarr[0], **kwargs)
        axarr[0].set_ylabel('mm/h')
        axarr[0].set_title(r'precipitation intensity, $\alpha=%s, \beta=%s$' 
                            % (self.ab[0], self.ab[1]))
        rho = self.scale*self.density(fltr=False)
        rho.plot(label='mean density', ax=axarr[1])      
        axarr[1].set_ylabel(r'$\rho_{part}$')
        self.n_t().plot(ax=axarr[2])
        axarr[2].set_ylabel(r'$N_{tot} (m^{-3})$')
        self.d_m().plot(ax=axarr[3])
        axarr[3].set_ylabel(r'$D_m$ (mm)')
        axarr[0].legend(loc='upper right')
        axarr[-1].set_xlabel('time (UTC)')
        plt.show()
    
    def plot_cost(self, resolution=20, ax=None, cmap='binary', **kwargs):
        """The slowest plot you've made"""
        if self.ab is None:
            return
        if ax is None:
            ax = plt.gca()
        alpha0 = self.ab[0]
        alpha = np.linspace(0.4*alpha0, 1.4*alpha0, num=resolution)
        beta = np.linspace(self.bnd[1][0], self.bnd[1][1], num=resolution)
        z = np.zeros((alpha.size, beta.size))
        for i, a in enumerate(alpha):
            for j, b in enumerate(beta):
                z[i][j] = self.cost((a, b))
        ax = plt.gca()
        heat = ax.pcolor(beta, alpha, z, cmap=cmap, **kwargs)
        ax.colorbar()
        ax.set_xlabel(r'$\beta$')
        ax.set_ylabel(r'$\alpha$')
        ax.axis('tight')
        ax.set_title('cost function value')
        return z, heat, ax.plot(self.ab[1], self.ab[0], 'ro')
        
    def plot_cost_lsq(self, resolution, ax=None, *args, **kwargs):
        """Plot cost function value vs. beta."""
        if ax is None:
            ax = plt.gca()
        beta = np.linspace(self.bnd[1][0],self.bnd[1][1],num=resolution)
        cost = np.array([self.cost_lsq(b) for b in beta])
        ax =  plt.gca()
        ax.set_xlabel(r'$\beta$')
        ax.set_ylabel('cost')
        ax.set_title('cost function value')
        return ax.plot(beta, cost, *args, **kwargs)
        
    def xcorr(self, rule='1min', ax=None, **kwargs):
        """Plot cross-correlation between lwc estimate and pluvio rainrate. 
        Extra arguments are passed to pyplot.xcorr.
        """
        if ax is None:
            ax = plt.gca()
        r = self.pluvio.rainrate(rule, unbias=False)
        lwc = self.pipv.lwc(rule).reindex(r.index).fillna(0)
        return ax.xcorr(lwc, r, **kwargs)
        
    def autoshift(self, rule='1min', inplace=False):
        """Find and correct pluvio time shift using cross correlation."""
        if self.pluvio.shift_periods != 0:
            print('Pluvio already timeshifted, resetting.')
            self.pluvio.shift_reset()
        xc = self.xcorr(rule=rule)
        imaxcorr = xc[1].argmax()
        periods = xc[0][imaxcorr]
        if inplace:
            self.pluvio.shift_periods = periods
            self.pluvio.shift_freq = rule
            print('Pluvio timeshift set to %s*%s.' 
                % (str(self.pluvio.shift_periods), self.pluvio.shift_freq))
        return periods

class Snow2:
    """UNTESTED. 
    Calculate snowfall rate using Szyrmer Zawadski's method from Snow Study II.
    """
    def __init__(self):
        return

    @staticmethod
    def best(re, mh=True):
        if mh: # MH05
            cl = np.array([3.8233, -1.5211, 0.30065, -0.06104, 0.13074, -0.073429, 0.016006, -0.0012483])
        else: # KC05
            cl = np.array([3.8816, -1.4579, 0.27749, -0.41521, 0.57683, -0.29220, 0.06467, -0.0053405])    
        logx = 0    
        
        for l, c in enumerate(cl):
            logx += c*np.log(re)**l
        
        return np.exp(logx)
        
    @staticmethod
    def mass(u, ar, d):
        g = 9.81
        fa = 1
        rho_a = 1.275
        nu_a = 1.544e-5
        
        re = u*d/nu_a
        return np.pi*rho_a*nu_a**2/(8*g)*Snow2.best(re)*ar*fa

