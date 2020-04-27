# pylint: disable=C0103, R0902, R0903
"""Library to solve the time dependent schrodinger equation in the many-body Fock space.

This module contains the core computations
"""
from typing import Dict, Any, Tuple, List

import hashlib
import pickle

from numpy import ndarray
import numpy as np
from numpy.linalg import eigh

from scipy.integrate import solve_ivp
from scipy.linalg import logm

from qlp.tdse.schedule import AnnealSchedule

from qlpdb.graph.models import Graph
from qlpdb.tdse.models import Tdse

from django.core.files.base import ContentFile


def _set_up_pauli():
    """Creates Pauli matrices and identity
    """
    sigx = np.zeros((2, 2))
    sigz = np.zeros((2, 2))
    id2 = np.identity(2)
    proj0 = np.zeros((2, 2))
    proj1 = np.zeros((2, 2))
    sigx[0, 1] = 1.0
    sigx[1, 0] = 1.0
    sigz[0, 0] = 1.0
    sigz[1, 1] = -1.0
    proj0[0, 0] = 1.0
    proj1[1, 1] = 1.0
    return id2, sigx, sigz, proj0, proj1


ID2, SIG_X, SIG_Z, PROJ_0, PROJ_1 = _set_up_pauli()


class PureSolutionInterface:
    """Interface for a pure state solution

    Attributes:
        t: array
        y: array
    """

    def __init__(self, y1):
        self.t = np.zeros((0))
        self.y = np.zeros((y1.size, 0))


def convert_params(params):
    for key in params:
        if key in ["hi_for_offset", "hi"]:
            params[key] = list(params[key])
        elif key in ["Jij"]:
            params[key] = [list(row) for row in params["Jij"]]
    return params


class TDSE:
    """Time dependent Schrödinger equation solver class

    .. code-block:: python

        tdse = TDSE(n, ising_params, offset_params, solver_params)

        # Get thermodynamical state density
        temperature=15e-3
        initial_wavefunction = "true"
        rho = tdse.init_densitymatrix(temperature, initial_wavefunction)

        # Compute anneal
        sol_densitymatrix = tdse.solve_mixed(rho)
    """

    def __init__(
        self,
        graph_params: Dict[str, Any],
        ising_params: Dict[str, Any],
        offset_params: Dict[str, Any],
        solver_params: Dict[str, Any],
    ):
        """Init the class with

        Arguments:
            graph_params: Parameters of input graph
            ising_params: Parameters for the ising model, e.g., keys are
                {"Jij", "hi", "c", "energyscale"}.
            offset_params: Parameters for AnnealSchedule
            solver_params: Parameters for solve_ivp
        """
        self.graph = graph_params
        self.ising = ising_params
        self.offset = offset_params
        self.solver = solver_params
        self.FockX, self.FockZ, self.FockZZ, self.Fockproj0, self.Fockproj1 = (
            self._init_Fock()
        )
        self.Focksize = None
        self.AS = AnnealSchedule(**offset_params, graph_params=graph_params)
        self.IsingH = self._constructIsingH(
            self._Bij(self.AS.B(1)) * self.ising["Jij"], self.AS.B(1) * self.ising["hi"]
        )

    def hash_dict(self, d):
        hash = hashlib.md5(
            str([[key, d[key]] for key in sorted(d)]).replace(" ", "").encode("utf-8")
        ).hexdigest()
        return hash

    def summary(
        self, wave_params, instance, solution, time, probability, entropy_params, entropy
    ):
        """
        output dictionary used to store tdse run into EspressodB

        tag: user-defined string (e.g. NN(3)_negbinary_-0.5_1.0_mixed_
        penalty: strength of penalty for slack variables
        ising_params: Jij, hi, c, energyscale (unit conversion from GHz)
        solver_params: method, rtol, atol
        offset_params:normalized_time, offset, hi_for_offset, offset_min, offset_range, fill_value, anneal_curve
        wave_params: pure or mixed, temp, initial_wavefunction. If pure, temp = 0
        instance: instance of tdse class
        time: solver time step
        probability: prob of Ising ground state
        nA: number of qubits in partition A
        indicesA: einsum notation
        entropy: entropy between partition A and B
        """
        # make tdse inputs
        tdse_params = dict()
        wf_type = wave_params["type"]
        a_time = self.offset["annealing_time"]
        offset_type = self.offset["offset"]
        omin = self.offset["offset_min"]
        orange = self.offset["offset_range"]
        tdse_params["tag"] = f"{wf_type}_{a_time}us_{offset_type}_{omin}_{orange}"
        ising = dict(self.ising)
        ising["Jij"] = [list(row) for row in ising["Jij"]]
        ising["hi"] = list(ising["hi"])
        tdse_params["ising"] = ising
        tdse_params["ising_hash"] = self.hash_dict(tdse_params["ising"])
        offset = dict(self.offset)
        offset["hi_for_offset"] = list(offset["hi_for_offset"])
        tdse_params["offset"] = offset
        tdse_params["offset_hash"] = self.hash_dict(tdse_params["offset"])
        solver = dict(self.solver)
        tdse_params["solver"] = solver
        tdse_params["solver_hash"] = self.hash_dict(tdse_params["solver"])
        tdse_params["wave"] = wave_params
        tdse_params["wave_hash"] = self.hash_dict(tdse_params["wave"])
        tdse_params["time"] = list(time)
        tdse_params["prob"] = list(probability)
        tdse_params["entropy_params"] = entropy_params
        tdse_params["entropy_params_hash"] = self.hash_dict(
            tdse_params["entropy_params"]
        )
        tdse_params["entropy"] = list(entropy)

        # select or insert row in graph
        gp = {key: self.graph[key] for key in self.graph if key not in ["total_qubits"]}
        graph, _ = Graph.objects.get_or_create(**gp)
        # select or insert row in tdse
        tdse, _ = Tdse.objects.get_or_create(graph=graph, **tdse_params)
        # save pickled class instance
        content = pickle.dumps(instance)
        fid = ContentFile(content)
        tdsehash = self.hash_dict(
            {
                "ising": tdse_params["ising_hash"],
                "offset": tdse_params["offset_hash"],
                "solver": tdse_params["solver_hash"],
                "wave": tdse_params["wave_hash"],
                "entropy": tdse_params["entropy_params_hash"],
            }
        )
        tdse.instance.save(tdsehash, fid)
        fid.close()
        # save pickled solution
        content = pickle.dumps(solution)
        fid = ContentFile(content)
        tdse.solution.save(tdsehash, fid)
        fid.close()

        return tdse

    def _apply_H(self, t, psi: ndarray) -> ndarray:
        """Computes `i H(t) psi`"""
        return -1j * np.dot(self.annealingH(t), psi)

    def ground_state_degeneracy(
        self, H: ndarray, degeneracy_tol: float = 1e-6, debug: bool = False
    ) -> Tuple[ndarray, ndarray, ndarray]:
        """Computes the number of degenerate ground states

        Identifies degeneracy by comparing closeness to smallest eigenvalule.

        Arguments:
            H: Hamiltonian to compute eigen vectors off
            degeneracy_tol: Precision of comparison to GS
            debug: More output

        Returns: Ids for gs vectors, all eigenvalues, all eigenvectors
        """
        eigval, eigv = eigh(H)
        mask = [abs((ei - eigval[0]) / eigval[0]) < degeneracy_tol for ei in eigval]
        gs_idx = np.arange(len(eigval))[mask]
        if debug:
            print(
                f"Num. degenerate states @"
                f" s={self.offset['normalized_time'][1]}: {len(gs_idx)}"
            )
        return gs_idx, eigval, eigv

    @staticmethod
    def calculate_overlap(psi1: ndarray, psi2: ndarray, degen_idx: List[int]) -> float:
        """Computes overlaps of states in psi1 with psi2 (can be multiple)

        Overlap is defined as ``sum(<psi1_i | psi2>, i in degen_idx)``. This allows to
        compute overlap in presence of degeneracy. See als eq. (57) in the notes.

        Arguments:
            psi1: Set of wave vectors. Shape is (size_vector, n_vectors).
            psi2: Vector to compare against.
            degen_idx: Index for psi1 vectors.

        """
        return sum(
            np.absolute([np.dot(np.conj(psi1[:, idx]), psi2) for idx in degen_idx]) ** 2
        )

    def init_eigen(self, dtype: str) -> Tuple[ndarray, ndarray]:
        """Computes eigenvalue and vector of initial Hamiltonian either as a pure
        eigenstate of `H_init` (transverse) or a s a superposition of
        `A(s) H_init + B(s) H_final` (true).
        """
        if dtype == "true":
            # true ground state
            eigvalue, eigvector = eigh(
                self.annealingH(s=self.offset["normalized_time"][0])
            )
        elif dtype == "transverse":
            # DWave initial wave function
            eigvalue, eigvector = eigh(
                -1
                * self.ising["energyscale"]
                * self._constructtransverseH(
                    self.AS.A(0) * np.ones(self.graph["total_qubits"])
                )
            )
        else:
            raise TypeError("Undefined initial wavefunction.")
        return eigvalue, eigvector

    def init_wavefunction(self, dtype="transverse") -> ndarray:
        """Returns wave function for first eigenstate of Hamiltonian of dtype.
        """
        _, eigvector = self.init_eigen(dtype)
        return (1.0 + 0.0j) * eigvector[:, 0]

    def init_densitymatrix(
        self, temp: float = 13e-3, dtype: str = "transverse", debug: bool = False
    ) -> ndarray:
        """Returns density matrix for s=0

        ``rho(s=0) = exp(- beta H(0)) / Tr(exp(- beta H(0)))``


        Arguments:
            temp: Temperature in K
            dtype: Kind of inital wave function (true or transverse)
            debug: More output messages
        """
        kb = 8.617333262145e-5  # Boltzmann constant [eV / K]
        h = 4.135667696e-15  # Plank constant [eV s] (no 2 pi)
        one = 1e-9  # GHz s
        beta = 1 / (temp * kb / h * one)  # inverse temperature [h/GHz]

        # construct initial density matrix
        eigvalue, eigvector = self.init_eigen(dtype)

        dE = eigvalue[:] - eigvalue[0]
        pr = np.exp(-beta * dE)
        pr = pr / sum(pr)
        if debug:
            print("dE", dE)
            print("pr", pr, "total", sum(pr))

        rho = np.zeros((eigvalue.size * eigvalue.size))
        for i in range(eigvalue.size):
            rho = rho + (pr[i]) * np.kron(eigvector[:, i], np.conj(eigvector[:, i]))
        rho = (1.0 + 0.0j) * rho
        return rho

    def _init_Fock(self) -> Tuple[ndarray, ndarray, ndarray]:
        r"""Computes pauli matrix tensor products

        Returns:
            ``sigma^x_i \otimes 1``,
            ``sigma^z_i \otimes 1``,
            ``sigma^z_i \otimes sigma^z_j \otimes 1``
        """
        FockX = [self.pushtoFock(i, SIG_X) for i in range(self.graph["total_qubits"])]
        FockZ = [self.pushtoFock(i, SIG_Z) for i in range(self.graph["total_qubits"])]
        FockZZ = [
            [np.dot(FockZ[i], FockZ[j]) for j in range(self.graph["total_qubits"])]
            for i in range(self.graph["total_qubits"])
        ]
        Fockproj0 = [
            self.pushtoFock(i, PROJ_0) for i in range(self.graph["total_qubits"])
        ]
        Fockproj1 = [
            self.pushtoFock(i, PROJ_1) for i in range(self.graph["total_qubits"])
        ]
        return FockX, FockZ, FockZZ, Fockproj0, Fockproj1

    def pushtoFock(self, i: int, local: ndarray) -> ndarray:
        """Tensor product of `local` at particle index i with 1 in fock space

        Arguments:
            i: particle index of matrix local
            local: matrix operator
        """
        fock = np.identity(1)
        for j in range(self.graph["total_qubits"]):
            if j == i:
                fock = np.kron(fock, local)
            else:
                fock = np.kron(fock, ID2)
        return fock

    def _constructIsingH(self, Jij: ndarray, hi: ndarray) -> ndarray:
        """Computes Hamiltonian (``J_ij is i < j``, i.e., upper diagonal"""
        IsingH = np.zeros(
            (2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"])
        )
        for i in range(self.graph["total_qubits"]):
            IsingH += hi[i] * self.FockZ[i]
            for j in range(i):
                IsingH += Jij[j, i] * self.FockZZ[i][j]
        return IsingH

    def _constructtransverseH(self, hxi: ndarray) -> ndarray:
        r"""Construct sum of tensor products of ``\sigma^x_i \otimes 1``
        """
        transverseH = np.zeros(
            (2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"])
        )
        for i in range(self.graph["total_qubits"]):
            transverseH += hxi[i] * self.FockX[i]
        return transverseH

    def _Bij(self, B: ndarray) -> ndarray:
        """J_ij coefficients for final Annealing Hamiltonian

        https://www.dwavesys.com/sites/default/files/
          14-1002A_B_tr_Boosting_integer_factorization_via_quantum_annealing_offsets.pdf

        Equation (1)

        Arguments:
            B: Anneal coefficients for given schedule.
        """
        return np.asarray(
            [
                [np.sqrt(B[i] * B[j]) for i in range(self.graph["total_qubits"])]
                for j in range(self.graph["total_qubits"])
            ]
        )

    def annealingH(self, s: float) -> ndarray:
        """Computes ``H(s) = A(s) H_init + B(s) H_final`` in units of "energyscale"
        """
        AxtransverseH = self._constructtransverseH(
            self.AS.A(s) * np.ones(self.graph["total_qubits"])
        )
        BxIsingH = self._constructIsingH(
            self._Bij(self.AS.B(s)) * self.ising["Jij"], self.AS.B(s) * self.ising["hi"]
        )
        H = self.ising["energyscale"] * (-1 * AxtransverseH + BxIsingH)
        return H

    def solve_pure(
        self, y1: ndarray, ngrid: int = 11, debug: bool = False
    ) -> PureSolutionInterface:
        """Solves time depepdent Schrödinger equation for pure inital state
        """
        start = self.offset["normalized_time"][0]
        end = self.offset["normalized_time"][1]
        interval = np.linspace(start, end, ngrid)

        sol = PureSolutionInterface(y1)

        for jj in range(ngrid - 1):
            y1 = y1 / (np.sqrt(np.absolute(np.dot(np.conj(y1), y1))))
            tempsol = solve_ivp(
                fun=self._apply_H,
                t_span=[interval[jj], interval[jj + 1]],
                y0=y1,
                t_eval=np.linspace(*self.offset["normalized_time"], num=100),
                **self.solver,
            )
            y1 = tempsol.y[:, tempsol.t.size - 1]
            sol.t = np.hstack((sol.t, tempsol.t))
            sol.y = np.hstack((sol.y, tempsol.y))

        if debug:
            print(
                "final total prob",
                (np.absolute(np.dot(np.conj(sol.y[:, -1]), sol.y[:, -1]))) ** 2,
            )
        return sol

    def _annealingH_densitymatrix(self, s: float) -> ndarray:
        """Tensor product of commutator of annealing Hamiltonian with id in Fock space

        Code:
            H(s) otimes 1 - 1 otimes H(s)
        """
        Fockid = np.identity(self.Focksize)
        return np.kron(self.annealingH(s), Fockid) - np.kron(Fockid, self.annealingH(s))

    def _apply_tdse_dense(self, t: float, y: ndarray) -> ndarray:
        """Computes ``-i [H(s), rho(s)]`` for density vector `y`"""
        f = -1j * np.dot(self._annealingH_densitymatrix(t), y)
        return f

    def _apply_tdse_dense2(self, t: float, y: ndarray) -> ndarray:
        """Computes ``-i [H(s), rho(s)]`` for density vector ``y`` by reshaping ``y``
        """
        # f = -1j * np.dot(self.annealingH_densitymatrix(t), y)
        # print('waht', type(self.Focksize))
        # print(self.Focksize)
        ymat = y.reshape((self.Focksize, self.Focksize))
        H = self.annealingH(t)
        ymat = -1j * (np.dot(H, ymat) - np.dot(ymat, H))
        f = ymat.reshape(self.Focksize ** 2)
        return f

    def solve_mixed(self, rho: ndarray) -> ndarray:
        """Solves the TDSE
        """
        self.Focksize = int(np.sqrt(len(rho)))
        sol = solve_ivp(
            fun=self._apply_tdse_dense2,
            t_span=self.offset["normalized_time"],
            y0=rho,
            t_eval=np.linspace(*self.offset["normalized_time"], num=100),
            **self.solver,
        )
        return sol

    # Compute Correlations
    # One time correlation function
    def cZ(self, ti, xi, sol_densitymatrix):
        return np.trace(
            np.dot(
                self.FockZ[xi],
                sol_densitymatrix.y[:, ti].reshape(
                    2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"]
                ),
            )
        )

    def c0(self, ti, xi, sol_densitymatrix):
        return np.trace(
            np.dot(
                self.Fockproj0[xi],
                sol_densitymatrix.y[:, ti].reshape(
                    2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"]
                ),
            )
        )

    def c1(self, ti, xi, sol_densitymatrix):
        return np.trace(
            np.dot(
                self.Fockproj1[xi],
                sol_densitymatrix.y[:, ti].reshape(
                    2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"]
                ),
            )
        )

    def cZZ(self, ti, xi, xj, sol_densitymatrix):
        return np.trace(
            np.dot(
                self.FockZZ[xi][xj],
                sol_densitymatrix.y[:, ti].reshape(
                    2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"]
                ),
            )
        )

    def cZZd(self, ti, xi, xj, sol_densitymatrix):
        return self.cZZ(ti, xi, xj, sol_densitymatrix) - self.cZ(
            ti, xi, sol_densitymatrix
        ) * self.cZ(ti, xj, sol_densitymatrix)

    # Two time correlation function
    # http://qutip.org/docs/latest/guide/guide-correlation.html
    def cZZt2(self, ti, xi, sol_densitymatrixt2):
        return np.trace(
            np.dot(
                self.FockZ[xi],
                sol_densitymatrixt2.y[:, ti].reshape(
                    2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"]
                ),
            )
        )

    def cZZt2d(self, ti, xi, tj, xj, sol_densitymatrix, sol_densitymatrixt2):
        return np.trace(
            np.dot(
                self.FockZ[xi],
                sol_densitymatrixt2.y[:, ti].reshape(
                    2 ** self.graph["total_qubits"], 2 ** self.graph["total_qubits"]
                ),
            )
        ) - self.cZ(ti, xi, sol_densitymatrix) * self.cZ(tj, xj, sol_densitymatrix)

    # entanglement entropy

    def ent_entropy(self, rho, nA, indicesA, reg):
        """
        calculate the entanglement entropy
        input:
           rho: density matrix
           n: number of qubits
           nA: number of qubits in partition A
           indicesA: einsum string for partial trace
           reg: infinitesimal regularization
        """
        tensorrho = rho.reshape(
            tuple([2 for i in range(2 * self.graph["total_qubits"])])
        )
        rhoA = np.einsum(indicesA, tensorrho)
        matrhoA = rhoA.reshape(2 ** nA, 2 ** nA) + reg * np.identity(2 ** nA)
        s = -np.trace(np.dot(matrhoA, logm(matrhoA) / np.log(2)))
        return s

    def find_partition(self) -> Tuple[int, str]:
        """
        Assumes that offset range is symmetric around zero.
        Splits partition to positive and negative offsets.
        Returns a np.einsum index string

        Example:
        nA = 2  # how many qubits in partition A
        indicesA = "ijmnijkl"  # einsum '1234 1234' qubits ie. if nA = 1 I trace out 3 out of 4 qubits
        """
        from string import ascii_lowercase as abc

        if self.offset["offset_min"] == 0:
            """
            If the offset if zero, partition same qubits as problems with offset.
            """
            op = dict(self.offset)
            op["offset_min"] = -0.1
            op["offset_range"] = 0.2
            A = AnnealSchedule(**op, graph_params=self.graph)
            offsets = A.offset_list
        else:
            offsets = self.AS.offset_list
        einidx1 = ""
        einidx2 = ""
        idx = 0
        nA = 0
        for offset in offsets:
            if offset < 0:
                einidx1 += abc[idx]
                einidx2 += abc[idx]
                idx += 1
            else:
                einidx1 += abc[idx]
                idx += 1
                einidx2 += abc[idx]
                idx += 1
                nA += 1
        einidx = einidx1 + einidx2
        return nA, einidx


"""
# CODE FOR KL DIVERGENCE
# copied from Jupyter to here

        # KL divergence
        from scipy import special
        from scipy.special import rel_entr

        # print('hello',rel_entr(np.zeros(2),np.zeros(2)))
        nt = 11
        tgrid = np.linspace(0, 1, nt)
        dimH = 2 ** n
        # print(n)
        KLdiv = np.zeros(nt)
        KLdiv2 = np.zeros(nt)
        KLdiv3 = np.zeros(nt)
        KLdiv4 = np.zeros(nt)
        for i in range(nt):
            energy, evec = np.linalg.eigh(tdse.annealingH(tgrid[i]))
            midn = int(dimH / 2)
            # print(midn)
            # the KL divergence between mid eigens state and nearby eigen state distribution
            p = np.absolute(np.conj(evec[:, midn]) * evec[:, midn])
            q = np.absolute(np.conj(evec[:, midn - 1]) * evec[:, midn - 1])
            KLdiv[i] = np.sum(rel_entr(p, q))
            KLdiv2[i] = np.sum(rel_entr(q, p))

            # the KL divergence between 1st and gnd state distribution
            p = np.absolute(np.conj(evec[:, 0]) * evec[:, 0])
            q = np.absolute(np.conj(evec[:, 1]) * evec[:, 1])
            KLdiv3[i] = np.sum(rel_entr(p, q))
            KLdiv4[i] = np.sum(rel_entr(q, p))

        plt.figure()
        plt.plot(tgrid, KLdiv)
        plt.plot(tgrid, KLdiv2)
        plt.plot(tgrid, KLdiv3)
        plt.plot(tgrid, KLdiv4)
        plt.legend(['mid/mid-1', 'mid-1/mid', 'gnd/1st', '1st/gnd'])
        plt.title('KL divergence')
        # end KL divergence

        # correlation functions
        xgrid = np.arange(n)
        # print(qubo.todense())

        plt.figure()
        data = np.asarray(
            [
                [
                    np.absolute(tdse.cZ(t, xgrid[x], sol_densitymatrix))
                    for t in range(sol_densitymatrix.t.size)
                ]
                for x in range(n)
            ]
        )

        # how correlated with sigma_z
        plt.figure("Z_i")
        ax = plt.axes([0.15, 0.15, 0.8, 0.8])
        for idx, datai in enumerate(data):
            ax.errorbar(x=sol_densitymatrix.t, y=datai, label=f"qubit {idx}")
        ax.set_title(r"$|< Z_i >|$")
        ax.set_xlabel(r"$t_i$")
        ax.set_ylabel(r"$x_i$")
"""
"""
# PLOT CORRELATION FUNCTIONS
        plt.figure()
        data = np.asarray(
            [
                [
                    np.absolute(tdse.cZZ(t, xj, xgrid[x], sol_densitymatrix))
                    for t in range(sol_densitymatrix.t.size)
                ]
                for x in range(n)
            ]
        )
        fig = plt.figure()
        ax = fig.add_subplot(111)
        pos = ax.imshow(data)
        ax.set_aspect("auto")
        ax.set_title(r"$|< Z_i Z_j >|$")
        ax.set_xlabel(r"$t_i$")
        ax.set_ylabel(r"$x_i$")
        fig.colorbar(pos)

        plt.figure()
        data = np.asarray(
            [
                [
                    np.absolute(tdse.cZZd(t, xgrid[x], xj, sol_densitymatrix))
                    for t in range(sol_densitymatrix.t.size)
                ]
                for x in range(n)
            ]
        )
        fig = plt.figure()
        ax = fig.add_subplot(111)
        pos = ax.imshow(data)
        ax.set_aspect("auto")
        ax.set_title(r"$|< Z_i Z_j>-< Z_i >< Z_j >|$")
        ax.set_xlabel(r"$t_i$")
        ax.set_ylabel(r"$x_i$")
        fig.colorbar(pos)


        plt.figure()
        t = 0
        data = np.asarray(
            [
                [
                    np.absolute(tdse.cZZd(t, xgrid[xi], xgrid[xj], sol_densitymatrix))
                    for xi in range(n)
                ]
                for xj in range(n)
            ]
        )
        fig = plt.figure()
        ax = fig.add_subplot(111)
        pos = ax.imshow(data)
        ax.set_aspect("auto")
        ax.set_title(r"$|< Z_i Z_j>-< Z_i >< Z_j >|$, initial")
        ax.set_xlabel(r"$x_j$")
        ax.set_ylabel(r"$x_i$")
        fig.colorbar(pos)

        plt.figure()
        t = sol_densitymatrix.t.size - 1
        data = np.asarray(
            [
                [
                    np.absolute(tdse.cZZd(t, xgrid[xi], xgrid[xj], sol_densitymatrix))
                    for xi in range(n)
                ]
                for xj in range(n)
            ]
        )
        fig = plt.figure()
        ax = fig.add_subplot(111)
        pos = ax.imshow(data)
        ax.set_aspect("auto")
        ax.set_title(r"$|< Z_i Z_j>-< Z_i >< Z_j >|$, final")
        ax.set_xlabel(r"$x_j$")
        ax.set_ylabel(r"$x_i$")
        fig.colorbar(pos)


        # two time correlation functions
        # need to solve again using different initial density matrix...

        # choose one xj... if you want other xj you have to do this for each xj
        tj = 0
        xj = 0
        rho2 = np.dot(tdse.FockZ[xj], rho.reshape(2 ** n, 2 ** n))
        sol_densitymatrixt2 = tdse.solve_mixed(rho2.reshape(4 ** n))

        plt.figure()
        data = np.asarray(
            [
                [
                    np.absolute(tdse.cZZt2(t, xgrid[x], sol_densitymatrixt2))
                    for t in range(sol_densitymatrix.t.size)
                ]
                for x in range(n)
            ]
        )
        fig = plt.figure()
        ax = fig.add_subplot(111)
        pos = ax.imshow(data)
        ax.set_aspect("auto")
        ax.set_title(r"$ double time |< Z_i(t) Z_j>|$")
        ax.set_xlabel(r"$t_i$")
        ax.set_ylabel(r"$x_i$")
        fig.colorbar(pos)

        plt.figure()
        data = np.asarray(
            [
                [
                    np.absolute(
                        tdse.cZZt2d(
                            t, xgrid[x], tj, xj, sol_densitymatrix, sol_densitymatrixt2
                        )
                    )
                    for t in range(sol_densitymatrix.t.size)
                ]
                for x in range(n)
            ]
        )
        fig = plt.figure()
        ax = fig.add_subplot(111)
        pos = ax.imshow(data)
        ax.set_aspect("auto")
        ax.set_title(r"$ double time |< Z_i(t) Z_j>-< Z_i(t) >< Z_j >|$")
        ax.set_xlabel(r"$t_i$")
        ax.set_ylabel(r"$x_i$")
        fig.colorbar(pos)
"""
