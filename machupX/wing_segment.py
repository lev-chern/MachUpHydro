
import json
import os
import warnings

import scipy.integrate as integ
import scipy.interpolate as interp
import matplotlib.pyplot as plt
import numpy as np
import math as m

from mpl_toolkits.mplot3d import Axes3D
from machupX.helpers import check_filepath, import_value, euler_to_quat, quat_inv_trans, quat_mult
from machupX.dxf import dxf
from airfoil_db.exceptions import DatabaseBoundsError


class WingSegment:
    """A class defining a segment of a lifting surface.

    Parameters
    ----------
    name : string
        Name of the wing segment.

    input_dict : dict
        Dictionary describing the geometry of the segment.

    side : string
        The side the wing segment is added on, either "right" or "left".

    unit_sys : str
        Default system of units.

    airfoil_dict : dict
        Dictionary of airfoil objects. Must contain the airfoils specified for this wing segment.

    origin : vector
        Origin (root) coordinates of the wing segment in body-fixed coordinates.

    Returns
    -------
    WingSegment
        Returns a newly created WingSegment object.

    Raises
    ------
    IOError
        If the input is improperly specified.
    """

    def __init__(self, name, input_dict, side, unit_sys, airfoil_dict, origin=[0.0, 0.0, 0.0]):

        self.name = name
        self._input_dict = input_dict
        self._unit_sys = unit_sys
        self.side = side
        self._origin = np.asarray(origin)

        self._attached_segments = {}
        self._getter_data = {}
        
        self.ID = self._input_dict.get("ID")
        if self.ID == 0 and name != "origin":
            raise IOError("Wing segment ID for {0} may not be 0.".format(name))
        if name == "origin":
            self.has_mirror = False
        else:
            self.has_mirror = self._input_dict.get("side", "both") == "both"
        
        self._initialize_params() # moved this statement outside of the if statement to force initilization for all segments, including the dummy origin one.
        if self.ID != 0: # These do not need to be run for the origin segment
            
            self._initialize_airfoils(airfoil_dict)
            self._initialize_getters()
            self._initialize_lifting_line()
            self._initialize_unit_vector_dists()
            self._setup_control_surface(self._input_dict.get("control_surface", None))

            # These make repeated calls for geometry information faster. Should be called again if geometry changes.
            self._setup_cp_data()
            self._setup_node_data()

            # Get CAD options
            self._cad_options = self._input_dict.get("CAD_options", {})
            

    def _initialize_params(self):

        # Determine if it's part of the main wing
        self.is_main = self._input_dict.get("is_main", False)

        # Shear dihedral
        self._shear_dihedral = self._input_dict.get("shear_dihedral", False)

        # Grid parameters
        grid_dict = self._input_dict.get("grid", {})
        self.N = grid_dict.get("N", 40)
        distribution = grid_dict.get("distribution", "cosine_cluster")
        flap_edge_cluster = grid_dict.get("flap_edge_cluster", True)
        extra_discont = grid_dict.get("cluster_points", [])
        self.reid_corr = grid_dict.get("reid_corrections", True)
        self.delta_joint = grid_dict.get("joint_length", 0.15)
        self.blend_dist = grid_dict.get("blending_distance", 1.0)

        # Get location information
        connect_dict = self._input_dict.get("connect_to", {})
        self._connected_to_ID = connect_dict.get("ID", 0)
        self._connected_to_loc = connect_dict.get("location", "tip")

        # Set origin offset
        self._delta_origin = np.zeros(3)
        self._delta_origin[0] = connect_dict.get("dx", 0.0)
        self._delta_origin[1] = connect_dict.get("dy", 0.0)
        self._delta_origin[2] = connect_dict.get("dz", 0.0)

        # Apply y-offset
        self.y_offset = connect_dict.get("y_offset", 0.0)
        
        if self.side == "left":
            self._delta_origin[1] -= self.y_offset
        else:
            self._delta_origin[1] += self.y_offset

        # Create arrays of span locations used to generate nodes and control points
        if distribution == "cosine_cluster": # Cosine clustering

            discont = []

            # Add flap edges
            if flap_edge_cluster:
                flap_dict = self._input_dict.get("control_surface", None)
                if flap_dict is not None:
                    discont.append(flap_dict.get("root_span", 0.0))
                    discont.append(flap_dict.get("tip_span", 1.0))

            # Add user-specified discontinuities
            for discont_span_frac in extra_discont:
                discont.append(discont_span_frac)

            # Ignore discontinuities at wingtip
            while True:
                try:
                    discont.remove(1.0)
                except ValueError:
                    break

            # Ignore discontinuities at wing root
            while True:
                try:
                    discont.remove(0.0)
                except ValueError:
                    break

            # Sort discontinuities
            discont.sort()
            discont.append(1.0) # I know this is kinda redundant, but it's the best thing I could think of
            discont.insert(0, 0.0)

            # Determine number of sections and number of control points in each section
            num_sec = len(discont)-1
            num_control_pts = []
            for i in range(num_sec):
                N = int(round(self.N*(discont[i+1]-discont[i])))
                num_control_pts.append(N)

            # Check all the points are accounted for
            diff = int(sum(num_control_pts)-self.N)
            if diff != 0:
                num_control_pts[0] -= diff # Use the root segment to make up the difference

            # Initialize span location storage
            node_span_locs = [0.0]
            cp_span_locs = []

            # Loop through sections (between clustering points); will iterate at least once
            for i in range(num_sec):

                # For Sections with no assigned control points, raise a warning and skip
                if num_control_pts[i] == 0:
                    warnings.warn("""Not enough control points for {0} to distribute between {1} and {2} percent span. Properties of this section will not factor into results. If undesired, increase number of control points or alter clustering.""".format(self.name, int(discont[i]*100), int(discont[i+1]*100)))
                    continue

                # Create node distribution
                node_theta_space = list(np.linspace(0.0, np.pi, num_control_pts[i]+1))
                for theta in node_theta_space[1:]:
                    s = 0.5*(1-np.cos(theta)) # Span fraction
                    node_span_locs.append(discont[i]+s*(discont[i+1]-discont[i]))

                # Create control point distribution
                cp_theta_space = np.linspace(np.pi/num_control_pts[i], np.pi, num_control_pts[i])-np.pi/(2*num_control_pts[i])
                for theta in cp_theta_space:
                    s = 0.5*(1-np.cos(theta)) # Span fraction
                    cp_span_locs.append(discont[i]+s*(discont[i+1]-discont[i]))

            # Convert to numpy arrays for faster manipulation later
            self.node_span_locs = np.array(node_span_locs)
            self.cp_span_locs = np.array(cp_span_locs)

        # Linear spacing
        elif distribution == "linear":
            self.node_span_locs = np.linspace(0.0, 1.0, self.N+1)
            self.cp_span_locs = np.linspace(1/(2*self.N), 1.0-1/(2*self.N), self.N)

        # User-specified distribution
        elif isinstance(distribution, list):

            # Check they've given the right number of points
            if len(distribution) != self.N*2+1:
                raise IOError("User specified distribution must have length of 2*N+1. Got length {0}; needed length {1}.".format(len(distribution), self.N*2+1))

            # Check we start at zero and end at one
            if distribution[0] != 0.0 or distribution[-1] != 1.0:
                raise IOError("User specified distribution must begin at 0 and end at 1.")

            # Check it is sorted
            if not (all(distribution[i]<distribution[i+1] for i in range(len(distribution)-1))): 
                raise IOError("User specified distribution must be monotonically increasing.")

            # Store
            self.node_span_locs = np.array(distribution[0::2])
            self.cp_span_locs = np.array(distribution[1::2])

        else:
            raise IOError("Distribution type {0} not recognized for wing segment {1}.".format(distribution, self.name))

        # In order to follow the airfoil sign convention (i.e. positive vorticity creates positive lift) 
        # node and control point locations must always proceed from left to right.
        if self.side == "left":
            self.node_span_locs = self.node_span_locs[::-1]
            self.cp_span_locs = self.cp_span_locs[::-1]


    def is_continuation(self):
        """Returns whether this wing segment directly connects to the tip of another wing segment."""

        # A continuation of a lifting line will attach to another wing segment at the tip with no offset
        return self._connected_to_ID != 0 and self._connected_to_loc == "tip" and np.linalg.norm(self._delta_origin) < 1e-12


    def _initialize_getters(self):
        # Sets getters for functions which are a function of span

        # Determine how the wing LQC has been given
        self.b = import_value("semispan", self._input_dict, self._unit_sys, "not_given")
        qc_loc_data = import_value("quarter_chord_locs", self._input_dict, self._unit_sys, "not_given")
        dihedral_data = import_value("dihedral", self._input_dict, self._unit_sys, "not_given")
        sweep_data = import_value("sweep", self._input_dict, self._unit_sys, "not_given")

        # Check for redundant definitions
        if isinstance(self.b, str) and not isinstance(qc_loc_data, np.ndarray):
            raise IOError("Either 'semispan' or 'quarter_chord_locs' must be specified.")
        if not isinstance(self.b, str) and isinstance(qc_loc_data, np.ndarray):
            raise IOError("'semispan' and 'quarter_chord_locs' may not both be specified at once.")
        if not isinstance(dihedral_data, str) and isinstance(qc_loc_data, np.ndarray):
            raise IOError("'dihedral' and 'quarter_chord_locs' may not both be specified at once.")
        if not isinstance(sweep_data, str) and isinstance(qc_loc_data, np.ndarray):
            raise IOError("'sweep' and 'quarter_chord_locs' may not both be specified at once.")

        # Perform various computations based on whether qc points are given
        if isinstance(qc_loc_data, np.ndarray):

            # Set flag
            self._qc_data_type = "points"

            # Determine the semispan of the wing
            self._qc_loc_data = np.zeros((qc_loc_data.shape[0]+1, 4))
            self._qc_loc_data[1:,1:] = qc_loc_data
            self.b = 0.0

            # Loop through points to add up semispan
            for i in range(self._qc_loc_data.shape[0]):

                # Skip the first
                if i == 0:
                    continue

                # Add on length of current segment
                self.b += np.linalg.norm((self._qc_loc_data[i,2:]-self._qc_loc_data[i-1,2:]).flatten())

                # Store current span location
                self._qc_loc_data[i,0] = self.b

            # Divide the span locations by the total span
            self._qc_loc_data[:,0] /= self.b

        else:

            # Set flag
            self._qc_data_type = "standard"

            # Store discontinuities to make the integrators more reliable
            self._discont = []

            # Restore defaults
            if isinstance(dihedral_data, str):
                dihedral_data = 0.0
            if isinstance(sweep_data, str):
                sweep_data = 0.0

        # Twist
        twist_data = import_value("twist", self._input_dict, self._unit_sys, 0.0)
        if callable(twist_data):
            self.get_twist = twist_data
        else:
            self.get_twist = self._build_getter_linear_f_of_span(twist_data, "twist", angular_data=True) # Side is not specified because the sign convention is the same for both

        # Dihedral
        if self._qc_data_type == "points":

            # Extract dihedral from qc points using central differencing
            def get_dihedral(span):

                # Convert input to array
                converted = False
                if isinstance(span, float):
                    converted = True
                    span = np.asarray(span)[np.newaxis]

                # Calculate dihedral
                dihedral = np.zeros_like(span)
                for i, s in enumerate(span):

                    # Get two points near span location of interest
                    if s < 0.005:
                        p0 = self._get_quarter_chord_loc(s)
                        p1 = self._get_quarter_chord_loc(s+0.01)
                    elif s > 0.995:
                        p0 = self._get_quarter_chord_loc(s-0.01)
                        p1 = self._get_quarter_chord_loc(s)
                    else:
                        p0 = self._get_quarter_chord_loc(s-0.005)
                        p1 = self._get_quarter_chord_loc(s+0.005)

                    # Calculate dihedral
                    dihedral[i] = np.arctan((p1[2]-p0[2])/(p1[1]-p0[1]))

                # Convert back to float if needed
                if converted:
                    span = span.item()
                    return dihedral.item()
                else:
                    return dihedral

            self.get_dihedral = get_dihedral

        elif callable(dihedral_data):

            # Get dihedral from user function
            def get_dihedral(s):
                if self.side == "left":
                    return dihedral_data(s)
                else:
                    return -dihedral_data(s)
            self.get_dihedral = get_dihedral

        else:

            # Create linear interpolator
            self.get_dihedral = self._build_getter_linear_f_of_span(dihedral_data, "dihedral", angular_data=True, flip_sign=(self.side=="right"))
            self._add_discontinuities(self._getter_data["dihedral"], self._discont)

        # Sweep
        if self._qc_data_type == "points":

            # Extract sweep from qc points using central differencing
            def get_sweep(span):

                # Convert input to array
                converted = False
                if isinstance(span, float):
                    converted = True
                    span = np.asarray(span)[np.newaxis]

                # Calculate sweep
                sweep = np.zeros_like(span)
                for i, s in enumerate(span):

                    # Get two points near span location of interest
                    if s < 0.005:
                        p0 = self._get_quarter_chord_loc(s)
                        p1 = self._get_quarter_chord_loc(s+0.01)
                    elif s > 0.995:
                        p0 = self._get_quarter_chord_loc(s-0.01)
                        p1 = self._get_quarter_chord_loc(s)
                    else:
                        p0 = self._get_quarter_chord_loc(s-0.005)
                        p1 = self._get_quarter_chord_loc(s+0.005)

                    # Calculate sweep
                    sweep[i] = -np.arctan((p1[0]-p0[0])/(np.sqrt((p1[1]-p0[1]))**2 + (p1[2]-p0[2])**2))

                # Convert back to float if needed
                if converted:
                    span = span.item()
                    return sweep.item()
                else:
                    return sweep

            self.get_sweep = get_sweep

        elif callable(sweep_data):

            # Get sweep from user function
            def get_sweep(s):
                if self.side == "left":
                    return -sweep_data(s)
                else:
                    return sweep_data(s)
            self.get_sweep = get_sweep

        else:

            # Create linear interpolator
            self.get_sweep = self._build_getter_linear_f_of_span(sweep_data, "sweep", angular_data=True, flip_sign=(self.side=="left"))
            self._add_discontinuities(self._getter_data["sweep"], self._discont)

        # Add 0.0 and 1.0 to discontinuities and sort
        if self._qc_data_type == "standard":
            if 0.0 not in self._discont:
                self._discont.append(0.0)
            if 1.0 not in self._discont:
                self._discont.append(1.0)
            self._discont = sorted(self._discont)

        # Chord
        chord_data = import_value("chord", self._input_dict, self._unit_sys, 1.0)

        if isinstance(chord_data, tuple): # Elliptic distribution
            self.get_chord = self._build_elliptic_chord_dist(chord_data[1])
        elif callable(chord_data):
            self.get_chord = chord_data
        else: # Linear distribution
            self.get_chord = self._build_getter_linear_f_of_span(chord_data, "chord")
            

    def _add_discontinuities(self, data, discont):
        # Finds discontinuities in the data (i.e. any change in linear distribution)

        if isinstance(data, np.ndarray):
            for i in range(data.shape[0]):
                if data[i,0].item() not in discont:
                    discont.append(data[i,0].item())


    def _build_getter_linear_f_of_span(self, data, name, angular_data=False, flip_sign=False):
        # Defines a getter function for data which is a function of span

        if isinstance(data, float): # Constant
            if angular_data:
                self._getter_data[name] = m.radians(data)
            else:
                self._getter_data[name] = data

            def getter(span):
                """
                span : float or ndarray
                    Non-dimensional span location.
                """

                # Make input an array
                converted = False
                if isinstance(span, float):
                    converted = True
                    span = np.asarray(span)[np.newaxis]

                # Reverse sign
                if flip_sign:
                    data = -np.full(span.shape, self._getter_data[name])
                else:
                    data = np.full(span.shape, self._getter_data[name])

                # Convert back to scalar if needed
                if converted:
                    span = span.item()
                    return data.item()
                else:
                    return data

        
        else: # Array
            if isinstance(data[0], np.void): # This will happen if the user inputs the array params as ints
                new_data = np.zeros((data.shape[0],2), dtype=float)
                for i in range(data.shape[0]):
                    new_data[i,0] = data[i][0]
                    new_data[i,1] = data[i][1]
                data = new_data

            self._getter_data[name] = np.copy(data)

            def getter(span):
                """
                span : float or ndarray
                    Non-dimensional span location.
                """

                # Convert input to array
                converted = False
                if isinstance(span, float):
                    converted = True
                    span = np.asarray(span)[np.newaxis]

                # Perform interpolation
                if angular_data:
                    data = np.interp(span, self._getter_data[name][:,0], np.radians(self._getter_data[name][:,1]))
                else:
                    data = np.interp(span, self._getter_data[name][:,0], self._getter_data[name][:,1])

                # Reverse data
                if flip_sign:
                    data = -data

                # Convert back to scalar if needed
                if converted:
                    span = span.item()
                    return data.item()
                else:
                    return data

        return getter


    def _build_elliptic_chord_dist(self, root_chord):
        # Creates a getter which will return the chord length as a function of span fraction according to an elliptic distribution
        self._root_chord = root_chord

        def getter(span_frac):
            return self._root_chord*np.sqrt(1-span_frac*span_frac)

        return getter


    def _initialize_unit_vector_dists(self):
        # Initializes distributions of unit normal, spanwise, and axial vectors for quick access later

        # Determine cumulative length along the LAC
        ac_loc = self._get_ll_loc(self.node_span_locs)
        d_ac_loc = np.diff(ac_loc, axis=0)
        ds = np.zeros(self.N+1)
        ds[1:] = np.cumsum(np.linalg.norm(d_ac_loc, axis=1))

        # Calculate unit spanwise vector
        gradient = np.gradient(ac_loc, ds, edge_order=2, axis=0)
        self._u_s_dist = gradient/np.linalg.norm(gradient, axis=1, keepdims=True)
        self._get_span_vec = interp.interp1d(self.node_span_locs, self._u_s_dist, axis=0)

        # Unit axial vector
        u_a_unswept = self._get_unswept_axial_vec(self.node_span_locs)
        k = np.einsum('ij,ij->i', self._u_s_dist, u_a_unswept)
        c1 = np.sqrt(1/(1-k*k))
        c2 = -c1*k
        u_a = c1[:,np.newaxis]*u_a_unswept+c2[:,np.newaxis]*self._u_s_dist
        self._u_a_dist = u_a/np.linalg.norm(u_a, axis=1, keepdims=True)
        self._get_axial_vec = interp.interp1d(self.node_span_locs, self._u_a_dist, axis=0)
        
        # Unit normal vector
        self._u_n_dist = np.cross(self._u_a_dist, self._u_s_dist)
        self._get_normal_vec = interp.interp1d(self.node_span_locs, self._u_n_dist, axis=0)


    def _initialize_airfoils(self, airfoil_dict):
        # Picks out the airfoils used in this wing segment and stores them. Also 
        # initializes airfoil coefficient getters

        # Get which airfoils are specified for this segment
        default_airfoil = list(airfoil_dict.keys())[0]
        airfoil = import_value("airfoil", self._input_dict, self._unit_sys, default_airfoil)

        self._airfoils = []
        self._airfoil_spans = []
        self._num_airfoils = 0

        # Setup data table
        if isinstance(airfoil, str): # Constant airfoil

            if not airfoil in list(airfoil_dict.keys()):
                raise IOError("'{0}' must be specified in 'airfoils'.".format(airfoil))

            # Just put the same airfoil at the root and the tip
            self._airfoils.append(airfoil_dict[airfoil])
            self._num_airfoils = 1
            self._airfoil_slices = [slice(0, self.N)]


        elif isinstance(airfoil, np.ndarray): # Distribution of airfoils

            # Store each airfoil and its span location
            for row in airfoil:

                name = row[1].item()

                try:
                    self._airfoils.append(airfoil_dict[name])
                except KeyError:
                    raise IOError("'{0}' must be specified in 'airfoils'.".format(name))

                self._airfoil_spans.append(float(row[0]))
                self._num_airfoils += 1

            # Determine control points within each airfoil span
            self._airfoil_slices = []
            if self.side == "right":
                prev_slice_end = 0
                for i, s in enumerate(self._airfoil_spans):
                    if i == 0:
                        continue

                    # Determine greatest control point index within this span
                    num_less = np.sum((self.cp_span_locs < s).astype(int))
                    self._airfoil_slices.append(slice(prev_slice_end, num_less))
                    prev_slice_end = num_less
            else:
                prev_slice_end = self.N
                for i, s in enumerate(self._airfoil_spans):
                    if i==0:
                        continue

                    # Determine smallest control point index withing this span
                    num_greater = np.sum((self.cp_span_locs > s).astype(int))
                    self._airfoil_slices.append(slice(num_greater, prev_slice_end))
                    prev_slice_end = num_greater

        else:
            raise IOError("Airfoil definition must a be a string or an array.")

        self._airfoil_spans = np.asarray(self._airfoil_spans)


    def _setup_control_surface(self, control_dict):
        # Sets up the control surface on this wing segment

        # These values are needed whether or not a control surface exists
        self._delta_flap = np.zeros(self.N) # Positive deflection is down
        self._cp_c_f = np.zeros(self.N)
        self._has_control_surface = False

        if control_dict is not None:
            self._has_control_surface = True

            self._control_mixing = {}

            # Determine which control points are affected by the control surface
            self._cntrl_root_span = control_dict.get("root_span", 0.0)
            self._cntrl_tip_span = control_dict.get("tip_span", 1.0)
            self._saturation_angle = np.radians(control_dict.get("saturation_angle", np.inf))
            self._cp_in_cntrl_surf = (self.cp_span_locs >= self._cntrl_root_span) & (self.cp_span_locs <= self._cntrl_tip_span)

            # Get chord data
            chord_data = import_value("chord_fraction", control_dict, self._unit_sys, 0.25)

            # Make sure endpoints line up
            if not isinstance(chord_data, float): # Array
                if chord_data[0,0] != self._cntrl_root_span or chord_data[-1,0] != self._cntrl_tip_span:
                    raise IOError("Endpoints of flap chord distribution must match specified root and tip span locations.")

            # Determine the flap chord fractions at each control point
            self.get_c_f = self._build_getter_linear_f_of_span(chord_data, "flap_chord_fraction")
            self._cp_c_f[self._cp_in_cntrl_surf] = self.get_c_f(self.cp_span_locs[self._cp_in_cntrl_surf])

            # Store mixing
            self._control_mixing = control_dict.get("control_mixing", {})
            is_sealed = control_dict.get("is_sealed", True)

            # TODO: Use sealed definition

            ## Determine flap efficiency for altering angle of attack
            #theta_f = np.arccos(2*self._cp_c_f-1)
            #eps_flap_ideal = 1-(theta_f-np.sin(theta_f))/np.pi

            ## Based off of Mechanics of Flight Fig. 1.7.4
            #hinge_eff = 3.9598*np.arctan((self._cp_c_f+0.006527)*89.2574+4.898015)-5.18786
            #if not is_sealed:
            #    hinge_eff *= 0.8

            #self._eta_h_eps_f = eps_flap_ideal*hinge_eff

            ## Determine flap efficiency for changing moment coef
            #self._Cm_delta_flap = (np.sin(2*theta_f)-2*np.sin(theta_f))/4


    def _setup_cp_data(self):
        """
        Creates and stores vectors of important data at each control point
        """
        
        self.u_a_cp = self._get_axial_vec(self.cp_span_locs)
        self.u_n_cp = self._get_normal_vec(self.cp_span_locs)
        self.u_s_cp = self._get_span_vec(self.cp_span_locs)
        self.u_a_cp_unswept = self._get_unswept_axial_vec(self.cp_span_locs)
        self.u_n_cp_unswept = self._get_unswept_normal_vec(self.cp_span_locs)
        self.u_s_cp_unswept = self._get_unswept_span_vec(self.cp_span_locs)
        self.c_bar_cp = self._get_cp_avg_chord_lengths()
        self.dihedral_cp = self.get_dihedral(self.cp_span_locs)
        self.sweep_cp = self.get_sweep(self.cp_span_locs)
        self.twist_cp = self.get_twist(self.cp_span_locs)
        self.dS = abs(self.node_span_locs[1:]-self.node_span_locs[:-1])*self.b*self.c_bar_cp

        # Store airfoil thickness and camber for swept section corrections
        max_cambers = np.zeros(self._num_airfoils)
        max_thicknesses = np.zeros(self._num_airfoils)
        for i in range(self._num_airfoils):
            max_cambers[i] = self._airfoils[i].get_max_camber()
            max_thicknesses[i] = self._airfoils[i].get_max_thickness()

        if self._num_airfoils == 1:
            self.max_camber_cp = np.ones(self.N)*max_cambers[0]
            self.max_thickness_cp = np.ones(self.N)*max_thicknesses[0]
        else:
            self.max_camber_cp = np.interp(self.cp_span_locs, self._airfoil_spans, max_cambers)
            self.max_thickness_cp = np.interp(self.cp_span_locs, self._airfoil_spans, max_thicknesses)


    def _setup_node_data(self):
        self.u_a_node = self._get_axial_vec(self.node_span_locs)
        self.c_node = self.get_chord(self.node_span_locs)

    
    def _initialize_lifting_line(self):
        # Sets up the lifting-line for this wing segment.

        # Get user offset
        ll_offset_data = import_value("ll_offset", self._input_dict, self._unit_sys, 0)

        # Set lifting-line on LAC as predicted by Kuchemann
        if ll_offset_data == "kuchemann":

            # If the sweep is not constant, don't calculate an offset
            kuchemann_invalid = False
            try:
                sweep_data = self._getter_data["sweep"]
                if not isinstance(sweep_data, float):
                    warnings.warn("Kuchemann's equations for the locus of aerodynamic centers cannot be used in the case of non-constant sweep. Reverting to no offset.")
                    ll_offset_data = 0.0
                    kuchemann_invalid = True

            except KeyError:

                # Check for constant sweep from given points
                if self._qc_data_type == "points":
                    spans = np.linspace(0.0, 1.0, 10)
                    sweeps = self.get_sweep(spans)
                    if not np.allclose(np.full(10, sweeps[0]), sweeps, rtol=1e-10, atol=1e-3):
                        warnings.warn("Kuchemann's equations for the locus of aerodynamic centers cannot be used in the case of non-constant sweep. Reverting to no offset.")
                        ll_offset_data = 0.0
                        kuchemann_invalid = True

                else:
                    warnings.warn("Kuchemann's equations for the locus of aerodynamic centers cannot be used in the case of non-constant sweep. Reverting to no offset.")
                    ll_offset_data = 0.0
                    kuchemann_invalid = True

            # Calculate offset as a fraction of the local chord
            if not kuchemann_invalid:
                
                # Get constants
                CLa_root = self._airfoils[0].get_CLa(alpha=0.0)
                area = integ.quad(lambda s : self.get_chord(s), 0, 1)[0]
                R_A = 2.0*self.b/area
                sweep = abs(self.get_sweep(0.0))

                # Calculate effective global wing sweep
                sweep_eff = sweep/((1+(CLa_root*m.cos(sweep)/(m.pi*R_A))**2)**0.25)

                # Calculate constants
                tan_k = m.tan(sweep_eff)
                try:
                    sweep_div = tan_k/sweep_eff
                except ZeroDivisionError:
                    sweep_div = 1.0
                exp = m.pi/(4.0*(m.pi+2.0*abs(sweep_eff)))
                K = (1+(CLa_root*m.cos(sweep_eff)/(m.pi*R_A))**2)**exp

                # Locations in span; we'll calculate the effective ac at the node locations and let MachUpX do linear interpolation to get to control point locations.
                if self.side == "left":
                    locs = np.copy(self.node_span_locs)[::-1]
                else:
                    locs = np.copy(self.node_span_locs)
                z = locs*self.b
                c = self.get_chord(locs)
                with np.errstate(divide='ignore', invalid='ignore'): # If the chord goes to zero at the tip
                    cen_inf = np.where(c != 0.0, z/c, 0.0)
                    tip_inf = np.where(c != 0.0, (self.b-z)/c, 0.0)

                # Get hyperbolic interpolation
                two_pi = 2.0*m.pi
                l_cen = np.sqrt(1+(two_pi*sweep_div*cen_inf)**2)-two_pi*sweep_div*cen_inf
                l_tip = np.sqrt(1+(two_pi*sweep_div*tip_inf)**2)-two_pi*sweep_div*tip_inf
                l = l_cen-l_tip

                # Calculate offset
                ll_offset = -(0.25*(1.0-1.0/K*(1.0+2.0*l*sweep_eff/m.pi)))

                # Assemble array
                ll_offset_data = np.concatenate((locs[:,np.newaxis], ll_offset[:,np.newaxis]), axis=1)

        # Create getter
        if callable(ll_offset_data):
            self._get_ll_offset = ll_offset_data
        else:
            self._get_ll_offset = self._build_getter_linear_f_of_span(ll_offset_data, "ll_offset")

        # Store control points
        self.control_points = self._get_ll_loc(self.cp_span_locs)

        # Store nodes on AC
        self.nodes = self._get_ll_loc(self.node_span_locs)


    def attach_wing_segment(self, new_segment_name, input_dict, side, unit_sys, airfoil_dict):
        """Attaches a wing segment to the current segment or one of its children.
        
        Parameters
        ----------
        new_segment_name : str
            Name of the wing segment to attach.

        input_dict : dict
            Dictionary describing the wing segment to attach.

        side : str
            Which side this wing segment goes on. Can only be "left" or "right"

        unit_sys : str
            The unit system being used. "English" or "SI".

        airfoil_dict : dict
            Dictionary of airfoil objects the wing segment uses to initialize its own airfoils.

        Returns
        -------
        WingSegment
            Returns a newly created wing segment.
        """

        # This can only be called on the origin segment
        if self.ID != 0:
            raise RuntimeError("Segments can only be added at the origin segment.")

        else:
            return self._attach_wing_segment(new_segment_name, input_dict, side, unit_sys, airfoil_dict)


    def _attach_wing_segment(self, new_segment_name, input_dict, side, unit_sys, airfoil_dict):
        # Recursive function for attaching a wing segment.

        connect_dict = input_dict.get("connect_to", {})
        parent_ID = connect_dict.get("ID", 0)

        # Check this ID matches the ID of the one we want to attach to
        if self.ID == parent_ID:

            # For mirrored wing segments, a right segment only ever attaches to a right segment and same with left
            if self.has_mirror and side not in self.name:
                return False
            
            # Determine the connection point
            if connect_dict.get("location", "tip") == "root":
                attachment_point = self.get_root_loc()
                
                # Remove y-offset
                if self.side == "left":
                    attachment_point[1] += self.y_offset
                else:
                    attachment_point[1] -= self.y_offset
            else:
                attachment_point = self.get_tip_loc()

            # Initialize wing segment
            self._attached_segments[new_segment_name] = WingSegment(new_segment_name, input_dict, side, unit_sys, airfoil_dict, attachment_point)

            # Set whether this segment's parent has a mirror
            self._attached_segments[new_segment_name].parent_has_mirror = self.has_mirror

            # Return reference to newly created wing segment
            return self._attached_segments[new_segment_name]

        else: # We need to recurse deeper

            result = False
            for segment_name, segment in self._attached_segments.items():

                result = segment._attach_wing_segment(new_segment_name, input_dict, side, unit_sys, airfoil_dict)

                if result is not False:
                    break

            if self.ID == 0 and not result:
                raise RuntimeError("Could not attach wing segment {0}. Check ID of parent is valid.".format(new_segment_name))

            return result


    def _get_attached_wing_segment(self, new_segment_name):
        # Returns a reference to the specified wing segment.
        try:
            # See if it is attached to this wing segment
            return self._attached_segments[new_segment_name]
        except KeyError:
            # Otherwise
            result = False
            for key in self._attached_segments:
                result = self._attached_segments[key]._get_attached_wing_segment(new_segment_name)
                if result:
                    break

            return result


    def get_root_loc(self):
        """Returns the location of the root quarter-chord.

        Returns
        -------
        ndarray
            Location of the root quarter-chord.
        """
        if self.ID == 0:
            return self._origin
        else:
            return self._origin+self._delta_origin


    def get_tip_loc(self):
        """Returns the location of the tip quarter-chord.

        Returns
        -------
        ndarray
            Location of the tip quarter-chord.
        """
        if self.ID == 0:
            return self._origin
        else:
            return self._get_quarter_chord_loc(1.0)


    def _get_quarter_chord_loc(self, span):
        #Returns the location of the quarter-chord at the given span fraction.
        if isinstance(span, float):
            converted = True
            span_array = np.asarray(span)[np.newaxis]
        else:
            converted = False
            span_array = np.asarray(span)

        if self._qc_data_type == "standard":

            # Integrate sweep and dihedral along the span to get the location
            ds = np.zeros((span_array.shape[0],3))
            for i, span in enumerate(span_array):
                for j, discont in enumerate(self._discont):

                    # Skip 0.0
                    if j == 0:
                        continue
                    else:
                        if span > discont:
                            ds[i,0] += integ.quad(lambda s : np.tan(self.get_sweep(s)), self._discont[j-1], discont)[0]*self.b
                            ds[i,1] += integ.quad(lambda s : -np.cos(self.get_dihedral(s)), self._discont[j-1], discont)[0]*self.b
                            ds[i,2] += integ.quad(lambda s : -np.sin(self.get_dihedral(s)), self._discont[j-1], discont)[0]*self.b
                        elif span <= discont:
                            ds[i,0] += integ.quad(lambda s : np.tan(self.get_sweep(s)), self._discont[j-1], span)[0]*self.b
                            ds[i,1] += integ.quad(lambda s : -np.cos(self.get_dihedral(s)), self._discont[j-1], span)[0]*self.b
                            ds[i,2] += integ.quad(lambda s : -np.sin(self.get_dihedral(s)), self._discont[j-1], span)[0]*self.b
                            break

            # Apply based on which side
            if self.side == "left":
                qc_loc = self.get_root_loc()+ds
            else:
                qc_loc = self.get_root_loc()-ds
        
        else:

            # Perform interpolation
            ds = np.zeros((span_array.shape[0],3))
            ds[:,0] = np.interp(span_array, self._qc_loc_data[:,0], self._qc_loc_data[:,1])
            if self.side == "left":
                ds[:,1] = -np.interp(span_array, self._qc_loc_data[:,0], self._qc_loc_data[:,2])
            else:
                ds[:,1] = np.interp(span_array, self._qc_loc_data[:,0], self._qc_loc_data[:,2])
            ds[:,2] = np.interp(span_array, self._qc_loc_data[:,0], self._qc_loc_data[:,3])

            # Apply to root location
            qc_loc = self.get_root_loc()+ds

        # Convert back
        if converted:
            qc_loc = qc_loc.flatten()

        return qc_loc


    def _get_unswept_axial_vec(self, span):
        # Returns the axial vector at the given span locations, not taking sweep into account
        if isinstance(span, float):
            span_array = np.asarray(span)[np.newaxis]
        else:
            span_array = np.asarray(span)

        twist = self.get_twist(span_array)
        dihedral = self.get_dihedral(span_array)
        
        C_twist = np.cos(twist)
        S_twist = np.sin(twist)
        C_dihedral = np.cos(dihedral)
        S_dihedral = np.sin(dihedral)

        return np.asarray([-C_twist, -S_twist*S_dihedral, S_twist*C_dihedral]).T


    def _get_unswept_normal_vec(self, span):
        # Returns the normal vector at the given span locations
        if isinstance(span, float):
            span_array = np.asarray(span)[np.newaxis]
        else:
            span_array = np.asarray(span)

        twist = self.get_twist(span_array)
        dihedral = self.get_dihedral(span_array)
        
        C_twist = np.cos(twist)
        S_twist = np.sin(twist)
        C_dihedral = np.cos(dihedral)
        S_dihedral = np.sin(dihedral)

        return np.asarray([-S_twist, C_twist*S_dihedral, -C_twist*C_dihedral]).T


    def _get_unswept_span_vec(self, span):
        # Returns the normal vector at the given span locations
        if isinstance(span, float):
            span_array = np.asarray(span)[np.newaxis]
        else:
            span_array = np.asarray(span)

        dihedral = self.get_dihedral(span_array)

        C_dihedral = np.cos(dihedral)
        S_dihedral = np.sin(dihedral)

        return np.asarray([np.zeros(span_array.size), C_dihedral, S_dihedral]).T


    def _get_ll_loc(self, span):
        # Returns the location of the lifting line at the given span fraction.
        if isinstance(span, float):
            single = True
            span = np.asarray(span)[np.newaxis]
        else:
            single = False
            span = np.asarray(span)

        loc = self._get_quarter_chord_loc(span)
        loc += (self._get_ll_offset(span)*self.get_chord(span))[:,np.newaxis]*self._get_unswept_axial_vec(span)
        if single:
            loc = loc.item()
        return loc


    def _get_cp_avg_chord_lengths(self):
        # Returns the average local chord length at each control point on the segment.
        node_chords = self.get_chord(self.node_span_locs)
        return (node_chords[1:]+node_chords[:-1])/2


    def _airfoil_interpolator(self, interp_spans, sample_spans, vals):
        # Interpolates the airfoil coefficients at the given span locations.
        # Allows for the coefficients having been evaluated as a function of 
        # span as well.
        # Solution found on stackoverflow
        i = np.arange(interp_spans.size)
        j = np.searchsorted(sample_spans, interp_spans) - 1
        j = np.where(j<0, 0, j) # Not allowed to go outside the array
        d = (interp_spans-sample_spans[j])/(sample_spans[j+1]-sample_spans[j])
        return_val = (1-d)*vals[i,j]+d*vals[i,j+1]
        return return_val


    def _get_control_point_coef(self, alpha, Rey, Mach, coef_func):
        """
        Determines the value of the desired coefficient at each control point
        """

        # Only one airfoil
        # NOTE: changed "self.airfoils" to "self._airfoils" to match vairbale names from above
        if self._num_airfoils == 1:
            try:
                return getattr(self._airfoils[0], coef_func)(alpha=alpha,
                                                             Rey=Rey,
                                                             Mach=Mach,
                                                             trailing_flap_deflection=self._delta_flap,
                                                             trailing_flap_fraction=self._cp_c_f)

            except DatabaseBoundsError as e:

                # Print out information
                print()
                print(e)
                print("Error occurred on wing {0}".format(self.name))
                print("{0:<20}{1:<20}{2:<20}{3:<20}{4:<20}".format("Span Fraction", "Alpha [deg]", "Re", "Flap Def. [deg]", "Flap Frac."))
                print("".join(["-"]*100))
                inputs = e.inputs_dict
                for i, alpha, Re, df, c_f in zip(e.exception_indices, np.degrees(inputs["alpha"]), inputs["Rey"], inputs["trailing_flap_deflection"], inputs["trailing_flap_fraction"]):
                    print("{0:<20.10}{1:<20.10}{2:<20.10}{3:<20.10}{4:<20.10}".format(self.cp_span_locs[i], alpha, Re, df, c_f))
                print()

                # Raise error to be caught by MachUpX error handler
                raise e 

        # Multiple airfoils
        else:

            try:

                # Create array of coefficients
                coefs = np.zeros((self.N, self._num_airfoils))
                for j, cur_slice in enumerate(self._airfoil_slices):
                    coefs[cur_slice,j] = getattr(self._airfoils[j], coef_func)(alpha=alpha[cur_slice],
                                                                               Rey=Rey[cur_slice],
                                                                               Mach=Mach[cur_slice],
                                                                               trailing_flap_deflection=self._delta_flap[cur_slice],
                                                                               trailing_flap_fraction=self._cp_c_f[cur_slice])
                    coefs[cur_slice,j+1] = getattr(self._airfoils[j+1], coef_func)(alpha=alpha[cur_slice],
                                                                                   Rey=Rey[cur_slice],
                                                                                   Mach=Mach[cur_slice],
                                                                                   trailing_flap_deflection=self._delta_flap[cur_slice],
                                                                                   trailing_flap_fraction=self._cp_c_f[cur_slice])

                # Interpolate
                return_coefs = self._airfoil_interpolator(self.cp_span_locs, self._airfoil_spans, coefs)
                return return_coefs
            except DatabaseBoundsError as e:
                # TODO Make useful error message here
                raise e


    def get_cp_CLa(self, alpha, Rey, Mach):
        """Returns the lift slope at each control point.

        Parameters
        ----------
        alpha : ndarray
            Angle of attack

        Rey : ndarray
            Reynolds number

        Mach : ndarray
            Mach number

        Returns
        -------
        float
            Lift slope
        """
            
        return self._get_control_point_coef(alpha, Rey, Mach, "get_CLa")


    def get_cp_aL0(self, Rey, Mach):
        """Returns the zero-lift angle of attack at each control point. Used for the linear 
        solution to NLL.

        Parameters
        ----------
        Rey : ndarray
            Reynolds number

        Mach : ndarray
            Mach number

        Returns
        -------
        float
            Zero lift angle of attack
        """

        return self._get_control_point_coef(np.zeros_like(Rey), Rey, Mach, "get_aL0") # Need to pass a dummy variable for alpha


    def get_cp_CLRe(self, alpha, Rey, Mach):
        """Returns the derivative of the lift coefficient with respect to Reynolds number at each control point

        Parameters
        ----------
        alpha : ndarray
            Angle of attack

        Rey : ndarray
            Reynolds number

        Mach : ndarray
            Mach number

        Returns
        -------
        float
            Z
        """

        return self._get_control_point_coef(alpha, Rey, Mach, "get_CLRe")


    def get_cp_CLM(self, alpha, Rey, Mach):
        """Returns the derivative of the lift coefficient with respect to Mach number at each control point

        Parameters
        ----------
        alpha : ndarray
            Angle of attack

        Rey : ndarray
            Reynolds number

        Mach : ndarray
            Mach number

        Returns
        -------
        float
            Z
        """

        return self._get_control_point_coef(alpha, Rey, Mach, "get_CLM")


    def get_cp_CL(self, alpha, Rey, Mach):
        """Returns the coefficient of lift at each control point as a function of params.

        Parameters
        ----------
        alpha : ndarray
            Angle of attack

        Rey : ndarray
            Reynolds number

        Mach : ndarray
            Mach number

        Returns
        -------
        float or ndarray
            Coefficient of lift
        """

        return self._get_control_point_coef(alpha, Rey, Mach, "get_CL")


    def get_cp_CD(self, alpha, Rey, Mach):
        """Returns the coefficient of drag at each control point as a function of params.

        Parameters
        ----------
        alpha : ndarray
            Angle of attack

        Rey : ndarray
            Reynolds number

        Mach : ndarray
            Mach number

        Returns
        -------
        float
            Coefficient of drag
        """

        return self._get_control_point_coef(alpha, Rey, Mach, "get_CD")


    def get_cp_Cm(self, alpha, Rey, Mach):
        """Returns the moment coefficient at each control point as a function of params.

        Parameters
        ----------
        alpha : ndarray
            Angle of attack

        Rey : ndarray
            Reynolds number

        Mach : ndarray
            Mach number

        Returns
        -------
        float
            Moment coefficient
        """

        return self._get_control_point_coef(alpha, Rey, Mach, "get_Cm")


    def get_outline_points(self):
        """Returns a set of points that represents the planar outline of the wing segment.
        
        Returns
        -------
        ndarray
            Array of outline points.
        """
        spans = np.linspace(0, 1, self.N)
        qc_points = self._get_quarter_chord_loc(spans)
        chords = self.get_chord(spans)
        axial_vecs = self._get_unswept_axial_vec(spans)

        points = np.zeros((self.N*2+1,3))

        # Leading edge
        points[:self.N,:] = qc_points - 0.25*(axial_vecs*chords[:,np.newaxis])

        # Trailing edge
        points[-2:self.N-1:-1,:] = qc_points + 0.75*(axial_vecs*chords[:,np.newaxis])

        # Complete the circle
        points[-1,:] = points[0,:]

        # Add control surface
        if self._has_control_surface:
            in_cntrl_surf = (spans >= self._cntrl_root_span) & (spans <= self._cntrl_tip_span)
            num_cntrl_points = np.sum(in_cntrl_surf)+2
            cntrl_points = np.zeros((num_cntrl_points,3))
            cntrl_points[1:num_cntrl_points-1,:] = (qc_points + (0.75-self.get_c_f(spans))[:,np.newaxis]*(axial_vecs*chords[:,np.newaxis]))[in_cntrl_surf]
            cntrl_points[0,:] = (qc_points + 0.75*(axial_vecs*chords[:,np.newaxis]))[in_cntrl_surf][0]
            cntrl_points[-1,:] = (qc_points + 0.75*(axial_vecs*chords[:,np.newaxis]))[in_cntrl_surf][-1]
        else:
            cntrl_points = None

        return points, cntrl_points


    def apply_control(self, control_state, control_symmetry):
        """Applies the control deflection in degrees to this wing segment's control surface deflection.

        Parameters
        ----------
        control_state : dict
            A set of key-value pairs where the key is the name of the control and the 
            value is the deflection. For positive mapping values, a positive deflection 
            here will cause a downward deflection of symmetric control surfaces and 
            downward deflection of the right surface for anti-symmetric control surfaces.
            Units may be specified as in the input file. Any deflections not given will 
            default to zero.

        control_symmetry : dict
            Specifies which of the controls are symmetric
        """
        if not self._has_control_surface:
            return # Don't even bother...

        # Determine flap deflection
        self._delta_flap = np.zeros(self.N)
        for key in self._control_mixing:

            # Get input
            deflection = import_value(key, control_state, self._unit_sys, 0.0)

            # Arrange distribution
            if isinstance(deflection, np.ndarray): # Variable deflection
                if deflection[0,0] != self._cntrl_root_span or deflection[-1,0] != self._cntrl_tip_span:
                    raise IOError("Endpoints of flap deflection distribution must match specified root and tip span locations.")
                new_deflection = np.zeros(self.N)
                new_deflection[self._cp_in_cntrl_surf] = np.interp(self.cp_span_locs[self._cp_in_cntrl_surf], deflection[:,0], deflection[:,1])
                deflection = new_deflection
            elif callable(deflection):
                deflection = deflection(self.cp_span_locs)

            # Check for distribution
            if self.side == "right" or control_symmetry[key]:
                self._delta_flap += deflection*self._control_mixing.get(key, 0.0)*self._cp_in_cntrl_surf
            else:
                self._delta_flap -= deflection*self._control_mixing.get(key, 0.0)*self._cp_in_cntrl_surf

        # Convert to radians
        self._delta_flap = np.radians(self._delta_flap)

        # Apply saturation
        self._delta_flap = np.where(self._delta_flap>self._saturation_angle, self._saturation_angle, self._delta_flap)
        self._delta_flap = np.where(self._delta_flap<-self._saturation_angle, -self._saturation_angle, self._delta_flap)


    def get_stl_vectors(self, **kwargs):
        """Calculates and returns the outline vectors required for 
        generating an .stl model of the wing segment.

        Parameters
        ----------
        section_resolution : int, optional
            Number of points to use in distcretizing the airfoil sections. Defaults to 200.

        close_te : bool, optional
            Whether to force the trailing edge to be sealed. Defaults to true

        Returns
        -------
        ndarray
            Array of outline vectors. First index is the facet index, second is the point
            index, third is the vector components.
        """

        # Determine params
        section_res = kwargs.get("section_resolution", 200)
        close_te = kwargs.get("close_te", True)
        close_root = self._cad_options.get("close_wing_root", False)
        close_tip = self._cad_options.get("close_wing_tip", False)
        round_root = self._cad_options.get("round_wing_root", False)
        round_tip = self._cad_options.get("round_wing_tip", False)
        if (round_root and close_root) or (round_tip and close_tip):
            raise IOError("Options to close or round the end of a wing segment may not both be selected. Please choose one or the other.")
        if round_tip or round_root:
            n_round = self._cad_options.get("n_rounding_sections", 10)
        else:
            n_round = 0

        # Initialize storage
        num_root_facets = (section_res//2-2)*2+close_root*(section_res%2)+close_te+round_root
        num_tip_facets = (section_res//2-2)*2+close_tip*(section_res%2)+close_te+round_tip
        num_facets = self.N*(section_res-1)*2+num_root_facets*close_root+num_tip_facets*close_tip+num_root_facets*n_round*round_root+num_tip_facets*n_round*round_tip
        vectors = np.zeros((num_facets*3,3))

        # Make sure we always go from root to tip
        if self.side == "right":
            node_span_locs = self.node_span_locs
        else:
            node_span_locs = self.node_span_locs[::-1]

        # Generate vectors
        for i in range(self.N):

            # Root-ward node
            root_span = node_span_locs[i]
            root_outline = self._get_airfoil_outline_coords_at_span(root_span, section_res, close_te)

            # Tip-ward node
            tip_span = node_span_locs[i+1]
            tip_outline = self._get_airfoil_outline_coords_at_span(tip_span, section_res, close_te)

            # Seal root
            if i == 0 and close_root:
                vectors[:num_root_facets*3] = self._get_stl_end_vectors(section_res, root_outline, close_te, num_root_facets, le_tri=section_res%2!=0)[::-1]

            # Seal tip
            if i == self.N-1 and close_tip:
                vectors[-num_tip_facets*3:] = self._get_stl_end_vectors(section_res, tip_outline, close_te, num_tip_facets, le_tri=section_res%2!=0)

            # Round root
            if i == 0 and round_root:
                d_theta = np.pi/n_round
                for j in range(n_round):
                    round_outline = self._get_round_outline(root_outline, d_theta*j, d_theta*(j+1), section_res, self.side=="right", abs(self.get_sweep(0.0)), False)[::-1]
                    if j == 0:
                        vectors[:num_root_facets*3] = self._get_stl_end_vectors(section_res, round_outline, close_te, num_root_facets, le_tri=True)
                    else:
                        vectors[num_root_facets*3*j:num_root_facets*3*(j+1)] = self._get_stl_end_vectors(section_res, round_outline, close_te, num_root_facets, le_tri=True)

            # Round tip
            if i == self.N-1 and round_tip:
                d_theta = np.pi/n_round
                for j in range(n_round):
                    round_outline = self._get_round_outline(tip_outline, d_theta*j, d_theta*(j+1), section_res, self.side=="left", abs(self.get_sweep(1.0)), True)
                    if j == n_round-1:
                        vectors[-(num_tip_facets*3)*(n_round-j):] = self._get_stl_end_vectors(section_res, round_outline, close_te, num_tip_facets, le_tri=True)
                    else:
                        vectors[-(num_tip_facets*3)*(n_round-j):-(num_tip_facets*3)*(n_round-j-1)] = self._get_stl_end_vectors(section_res, round_outline, close_te, num_tip_facets, le_tri=True)

            # Create facets between the outlines
            for j in range(section_res-1):

                # Check which side we're on
                # Rolling the order of the vertices based on this mirrors panel distributions for symmetric wings
                on_top = j <= section_res//2-1 # Calling this side top is totally arbitrary

                # Get index of these vertices
                index = (2*i*(section_res-1)+2*j)*3+num_root_facets*3*close_root+num_root_facets*3*n_round*round_root

                # Set orientation based on which span
                if self.side == "left":
                    if on_top:
                        vectors[index:index+6] = self._get_two_tris_from_quad(root_outline[j],
                                                                              root_outline[j+1],
                                                                              tip_outline[j+1],
                                                                              tip_outline[j])
                    else:
                        vectors[index:index+6] = self._get_two_tris_from_quad(root_outline[j+1],
                                                                              tip_outline[j+1],
                                                                              tip_outline[j],
                                                                              root_outline[j])
                else:
                    if on_top:
                        vectors[index:index+6] = self._get_two_tris_from_quad(tip_outline[j],
                                                                              tip_outline[j+1],
                                                                              root_outline[j+1],
                                                                              root_outline[j])
                    else:
                        vectors[index:index+6] = self._get_two_tris_from_quad(tip_outline[j+1],
                                                                              root_outline[j+1],
                                                                              root_outline[j],
                                                                              tip_outline[j])

        return vectors


    def _get_two_tris_from_quad(self, v0, v1, v2, v3):
        # Takes a set of four vertices and gives the two minimum AR triangles with the same orientation

        # Determine where to split
        x0 = np.linalg.norm(v0-v2) 
        x1 = np.linalg.norm(v1-v3)

        # Split along v0-v2
        if x0 < x1 or (x0 == x1 and self.side == "left"): # I don't get why this works, but it does
            return v0, v1, v2, v0, v2, v3
        
        # Split along v1-v3
        else:
            return v0, v1, v3, v1, v2, v3


    def _get_airfoil_outline_coords_at_span(self, span, N, close_te):
        # Returns the airfoil section outline in body-fixed coordinates at the specified span fraction with the specified number of points

        # Determine flap deflection and fraction at this point
        if self._has_control_surface and span >= self._cntrl_root_span and span <= self._cntrl_tip_span:
            if self.side == "left":
                d_f = np.interp(span, self.cp_span_locs[::-1], self._delta_flap[::-1])
            else:
                d_f = np.interp(span, self.cp_span_locs, self._delta_flap)
            c_f = self.get_c_f(span)
        else:
            d_f = 0.0
            c_f = 0.0

        # Linearly interpolate outlines, ignoring twist, etc for now
        if self._num_airfoils == 1:
            points = self._airfoils[0].get_outline_points(N=N, trailing_flap_deflection=d_f, trailing_flap_fraction=c_f, close_te=close_te)
        else:
            index = 0
            while True:
                if span >= self._airfoil_spans[index] and span <= self._airfoil_spans[index+1]:
                    total_span = self._airfoil_spans[index+1]-self._airfoil_spans[index]

                    # Get weights
                    root_weight = 1-abs(span-self._airfoil_spans[index])/total_span
                    tip_weight = 1-abs(span-self._airfoil_spans[index+1])/total_span

                    # Get outlines
                    root_outline = self._airfoils[index].get_outline_points(N=N, trailing_flap_deflection=d_f, trailing_flap_fraction=c_f, close_te=close_te)
                    tip_outline = self._airfoils[index+1].get_outline_points(N=N, trailing_flap_deflection=d_f, trailing_flap_fraction=c_f, close_te=close_te)

                    # Interpolate
                    points = root_weight*root_outline+tip_weight*tip_outline
                    break

                index += 1

        # Get twist, dihedral, and chord
        twist = self.get_twist(span)
        dihedral = self.get_dihedral(span)
        chord = self.get_chord(span)

        # Scale to chord and transform to body-fixed coordinates
        if self._shear_dihedral:
            q = euler_to_quat(np.array([0.0, twist, 0.0]))
        else:
            q_dih = euler_to_quat(np.array([dihedral, 0.0, 0.0]))
            q_twi = euler_to_quat(np.array([0.0, twist, 0.0]))
            q = quat_mult(q_dih, q_twi)

        untransformed_coords = chord*np.array([-points[:,0].flatten()+0.25, np.zeros(N), -points[:,1]]).T
        coords = self._get_quarter_chord_loc(span)[np.newaxis]+quat_inv_trans(q, untransformed_coords)

        return coords


    def _get_rectangle_outline_coords_at_span(self, span):
        # Returns the rectangle section outline in body-fixed coordinates at the specified span fraction

        # initialize rectangle outline
        rect = np.array([[1.0,-0.5],[1.0,0.5],[0.0,0.5],[0.0,-0.5],[1.0,-0.5]])

        # Linearly interpolate outlines, ignoring twist, etc for now
        if self._num_airfoils == 1:
            points = rect * 1.0
            points[:,1] = points[:,1] * self._airfoils[0].get_max_thickness()
        else:
            index = 0
            while True:
                if span >= self._airfoil_spans[index] and span <= self._airfoil_spans[index+1]:
                    total_span = self._airfoil_spans[index+1]-self._airfoil_spans[index]

                    # Get weights
                    root_weight = 1-abs(span-self._airfoil_spans[index])/total_span
                    tip_weight = 1-abs(span-self._airfoil_spans[index+1])/total_span

                    # Get outlines
                    root_outline = rect * 1.0
                    root_outline[:,1] = root_outline[:,1] * self._airfoils[index].get_max_thickness()
                    
                    tip_outline = rect * 1.0
                    tip_outline[:,1] = tip_outline[:,1] * self._airfoils[index+1].get_max_thickness()
                    
                    # Interpolate
                    points = root_weight*root_outline+tip_weight*tip_outline
                    break

                index += 1

        # Get twist, dihedral, and chord
        twist = self.get_twist(span)
        dihedral = self.get_dihedral(span)
        chord = self.get_chord(span)

        # Scale to chord and transform to body-fixed coordinates
        if self._shear_dihedral:
            q = euler_to_quat(np.array([0.0, twist, 0.0]))
        else:
            q_dih = euler_to_quat(np.array([dihedral, 0.0, 0.0]))
            q_twi = euler_to_quat(np.array([0.0, twist, 0.0]))
            q = quat_mult(q_dih, q_twi)

        untransformed_coords = chord*np.array([-points[:,0].flatten()+0.25, np.zeros(5), -points[:,1]]).T
        coords = self._get_quarter_chord_loc(span)[np.newaxis]+quat_inv_trans(q, untransformed_coords)

        return coords


    def _get_stl_end_vectors(self, N, outline_points, close_te, num_facets, le_tri):
        # Determines the stl vectors that seal an end of the wing segment

        # Initialize storage
        vectors = np.zeros((num_facets*3,3))

        # Create panels starting at trailing edge
        if close_te:
            vectors[0] = outline_points[0]
            vectors[1] = outline_points[1]
            vectors[2] = outline_points[-2]
            curr_vec_ind = 3
        else:
            vectors[:6] = self._get_two_tris_from_quad(outline_points[0],
                                                       outline_points[1],
                                                       outline_points[-2],
                                                       outline_points[-1])
            curr_vec_ind = 6

        # Loop through middle part
        for i in range(1, N//2-1):

            # Store vectors
            vectors[curr_vec_ind:curr_vec_ind+6] = self._get_two_tris_from_quad(outline_points[i],
                                                                                outline_points[i+1],
                                                                                outline_points[-(i+2)],
                                                                                outline_points[-(i+1)])

            # Increment index
            curr_vec_ind += 6

        # Handle triangle at leading edge
        if le_tri:
            vectors[curr_vec_ind] = outline_points[N//2-1]
            vectors[curr_vec_ind+1] = outline_points[N//2]
            vectors[curr_vec_ind+2] = outline_points[N//2+1]

        # Reorder vertices on the right side to keep the normal pointing outward
        # This is simpler than having side-dependent logic at each previous point
        if self.side == "right":
            t = np.copy(vectors[1::3])
            vectors[1::3] = np.copy(vectors[2::3])
            vectors[2::3] = np.copy(t)

        return vectors


    def _get_round_outline(self, orig_outline, theta_start, theta_end, N, rev_rot, sweep_mag, sweep_back):
        # Gives the outline points for a slice of the tip rounding

        # For even number of outline points, add a dummy point at the leading edge
        if N%2==0:
            p = 0.5*(orig_outline[N//2-1]+orig_outline[N//2])
            outline = np.insert(orig_outline, N//2, p, axis=0)
        else:
            outline = np.copy(orig_outline)

        # Determine section plane coordinate frame transformation matrix
        p0 = outline[N//2]
        p1 = outline[0]
        p2 = outline[N-N//4]
        T = np.zeros((3,3))
        T[0] = p1-p0
        T[0] /= np.linalg.norm(T[0])
        T[2] = np.cross(T[0], p2-p0)
        T[2] /= np.linalg.norm(T[2])
        T[1] = np.cross(T[2], T[0])

        # Transform the outline
        shifted_outline = outline-p0[np.newaxis,:]
        transed_outline = np.einsum('ij,kj->ki', T, shifted_outline)
        bottom_outline = transed_outline[-1:N//2-1:-1]
        top_outline = transed_outline[:N//2+1]

        # Get rotation matrices to start edge
        C_from_top_to_start = np.cos(theta_start)
        S_from_top_to_start = np.sin(theta_start)
        T_from_top_to_start = np.eye(3)
        T_from_top_to_start[1,1] = C_from_top_to_start
        T_from_top_to_start[1,2] = S_from_top_to_start
        T_from_top_to_start[2,1] = -S_from_top_to_start
        T_from_top_to_start[2,2] = C_from_top_to_start

        C_from_bottom_to_start = np.cos(np.pi-theta_start)
        S_from_bottom_to_start = np.sin(np.pi-theta_start)
        T_from_bottom_to_start = np.eye(3)
        T_from_bottom_to_start[1,1] = C_from_bottom_to_start
        T_from_bottom_to_start[1,2] = -S_from_bottom_to_start
        T_from_bottom_to_start[2,1] = S_from_bottom_to_start
        T_from_bottom_to_start[2,2] = C_from_bottom_to_start

        # Get rotation matrices to end edge
        C_from_top_to_end = np.cos(theta_end)
        S_from_top_to_end = np.sin(theta_end)
        T_from_top_to_end = np.eye(3)
        T_from_top_to_end[1,1] = C_from_top_to_end
        T_from_top_to_end[1,2] = S_from_top_to_end
        T_from_top_to_end[2,1] = -S_from_top_to_end
        T_from_top_to_end[2,2] = C_from_top_to_end

        C_from_bottom_to_end = np.cos(np.pi-theta_end)
        S_from_bottom_to_end = np.sin(np.pi-theta_end)
        T_from_bottom_to_end = np.eye(3)
        T_from_bottom_to_end[1,1] = C_from_bottom_to_end
        T_from_bottom_to_end[1,2] = -S_from_bottom_to_end
        T_from_bottom_to_end[2,1] = S_from_bottom_to_end
        T_from_bottom_to_end[2,2] = C_from_bottom_to_end

        # Reverse rotation
        if rev_rot:
            T_from_bottom_to_end = T_from_bottom_to_end.T
            T_from_bottom_to_start = T_from_bottom_to_start.T
            T_from_top_to_end = T_from_top_to_end.T
            T_from_top_to_start = T_from_top_to_start.T
        
        # Get weightings
        start_top_weight = 1.0-theta_start/np.pi
        start_bottom_weight = 1.0-start_top_weight
        end_top_weight = 1.0-theta_end/np.pi
        end_bottom_weight = 1.0-end_top_weight
        
        # Get new outlines
        start_outline = np.einsum('ij,kj->ki', T_from_top_to_start, top_outline)*start_top_weight+np.einsum('ij,kj->ki', T_from_bottom_to_start, bottom_outline)*start_bottom_weight
        end_outline = np.einsum('ij,kj->ki', T_from_top_to_end, top_outline)*end_top_weight+np.einsum('ij,kj->ki', T_from_bottom_to_end, bottom_outline)*end_bottom_weight

        # Ensure the rounding outlines do not protrude above or below the original outlines
        start_outline[:,1] = np.where(start_outline[:,1]<top_outline[:,1], top_outline[:,1], start_outline[:,1])
        start_outline[:,1] = np.where(start_outline[:,1]>bottom_outline[:,1], bottom_outline[:,1], start_outline[:,1])
        end_outline[:,1] = np.where(end_outline[:,1]<top_outline[:,1], top_outline[:,1], end_outline[:,1])
        end_outline[:,1] = np.where(end_outline[:,1]>bottom_outline[:,1], bottom_outline[:,1], end_outline[:,1])

        # Concatenate
        rounding_outline = np.concatenate((start_outline, end_outline[-2::-1]), axis=0)

        # Apply sweep
        offset = np.tan(sweep_mag)*np.abs(rounding_outline[:,2])
        if sweep_back:
            rounding_outline[:,0] += offset # Back in local coords is forward in global coords
        else:
            rounding_outline[:,0] -= offset

        # Transform to global coords
        return np.einsum('ij,ki->kj', T, rounding_outline)+p0[np.newaxis]


    def get_vtk_panel_vertices(self, **kwargs):
        """Calculates and returns a list of lists containing the vertices defining each panel for
        a vtk mesh.

        Parameters
        ----------
        section_resolution : int, optional
            Number of points to use in distcretizing the airfoil sections. Defaults to 200.

        close_te : bool, optional
            Whether to force the trailing edge to be sealed. Defaults to true

        Returns
        -------
        list
            Panel vertices.
        """

        # Determine params
        section_res = kwargs.get("section_resolution", 200)
        close_te = kwargs.get("close_te", True)
        close_root = self._cad_options.get("close_wing_root", False)
        close_tip = self._cad_options.get("close_wing_tip", False)
        round_root = self._cad_options.get("round_wing_root", False)
        round_tip = self._cad_options.get("round_wing_tip", False)
        if (round_root and close_root) or (round_tip and close_tip):
            raise IOError("Options to close or round the end of a wing segment may not both be selected. Please choose one or the other.")
        n_round = self._cad_options.get("n_rounding_sections", 10)

        # Initialize panel storage
        vertices = []

        # Seal root
        if close_root:
            outline = self._get_airfoil_outline_coords_at_span(0.0, section_res, close_te)
            if self.side=="left":
                outline = outline[::-1]
            vertices.extend(self._get_vtk_end_panels(section_res, outline, close_te))

        # Round root
        if round_root:
            outline = self._get_airfoil_outline_coords_at_span(0.0, section_res, close_te)
            d_theta = np.pi/n_round
            for j in range(n_round):
                round_outline = self._get_round_outline(outline, d_theta*j, d_theta*(j+1), section_res, self.side=="right", abs(self.get_sweep(0.0)), False)
                if self.side=="left":
                    round_outline = round_outline[::-1]
                if j == 0:
                    vertices.extend(self._get_vtk_end_panels(section_res, round_outline, close_te))
                else:
                    vertices.extend(self._get_vtk_end_panels(section_res, round_outline, close_te))

        # Seal tip
        if close_tip:
            outline = self._get_airfoil_outline_coords_at_span(1.0, section_res, close_te)
            if self.side=="right":
                outline = outline[::-1]
            vertices.extend(self._get_vtk_end_panels(section_res, outline, close_te))

        # Round tip
        if round_tip:
            outline = self._get_airfoil_outline_coords_at_span(1.0, section_res, close_te)
            d_theta = np.pi/n_round
            for j in range(n_round):
                round_outline = self._get_round_outline(outline, d_theta*j, d_theta*(j+1), section_res, self.side=="left", abs(self.get_sweep(1.0)), True)
                if self.side=="right":
                    round_outline = round_outline[::-1]
                if j == 0:
                    vertices.extend(self._get_vtk_end_panels(section_res, round_outline, close_te))
                else:
                    vertices.extend(self._get_vtk_end_panels(section_res, round_outline, close_te))

        # Generate panels over the surface of the wing
        for i in range(self.N):

            # Left spanwise node
            left_span = self.node_span_locs[i]
            left_outline = self._get_airfoil_outline_coords_at_span(left_span, section_res, close_te)

            # Right spanwise node
            right_span = self.node_span_locs[i+1]
            right_outline = self._get_airfoil_outline_coords_at_span(right_span, section_res, close_te)

            # Create panels between the outlines along the inside of the wing
            for j in range(section_res-1):
                vertices.append([right_outline[j], right_outline[j+1], left_outline[j+1], left_outline[j]])

        return vertices


    def _get_vtk_end_panels(self, N, outline_points, close_te):
        # Determines the stl vectors that seal an end of the wing segment

        # Initialize storage
        vertices = []

        # Create panels starting at trailing edge
        if close_te:
            vertices.append([outline_points[0], outline_points[1], outline_points[-2]])
        else:
            vertices.append([outline_points[0], outline_points[1], outline_points[-2], outline_points[-1]])

        # Loop through middle part
        for i in range(1, N//2-1):

            # Store vectors
            vertices.append([outline_points[i], outline_points[i+1], outline_points[-(i+2)], outline_points[-(i+1)]])

        # Handle triangle at leading edge
        if len(outline_points)%2!=0:
            vertices.append([outline_points[N//2-1], outline_points[N//2], outline_points[N//2+1]])

        return vertices


    def export_stp(self, **kwargs):
        """Creates a FreeCAD part representing a loft of the wing segment.

        Parameters
        ----------
        airplane_name: str
            Name of the airplane this segment belongs to.

        file_tag : str, optional
            Optional tag to prepend to output filename default. The output files will be named "<AIRCRAFT_NAME>_<WING_NAME>.stp".

        section_resolution : int
            Number of outline points to use for the sections. Defaults to 200.
        
        spline : bool, optional
            Whether the wing segment sections should be represented using splines. This can cause issues with some geometries/CAD 
            packages. Defaults to False.

        maintain_sections : bool, optional
            Whether the wing segment sections should be preserved in the loft. Defaults to True.

        close_te : bool, optional
            Whether to force the trailing edge to be sealed. Defaults to true
        """

        # Import necessary modules
        import FreeCAD
        import Part

        # Kwargs
        airplane_name = kwargs.get("airplane_name")
        file_tag = kwargs.get("file_tag", "")
        section_resolution = kwargs.get("section_resolution", 200)
        spline = kwargs.get("spline", False)
        maintain_sections = kwargs.get("maintain_sections", True)
        close_te = kwargs.get("close_te", True)

        # Create sections
        sections = []
        for s_i in self.node_span_locs:
            points = []

            # Get outline points
            outline = self._get_airfoil_outline_coords_at_span(s_i, section_resolution, close_te)

            # Check for wing going to a point
            if np.all(np.all(outline == outline[0,:])):
                #tip = FreeCAD.Base.Vector(*outline[0])
                #points.append(tip)
                #continue
                #TODO loft to an actual point
                outline = self._get_airfoil_outline_coords_at_span(s_i-0.000001, section_resolution, close_te)

            # Create outline points
            for point in outline:
                points.append(FreeCAD.Base.Vector(*point))

            # Add to section list
            if not spline: # Use polygon
                section_polygon = Part.makePolygon(points)
                sections.append(section_polygon)
            else: # Use spline
                section_spline = Part.BSplineCurve(points)
                sections.append(section_spline.toShape())

        # Loft
        wing_loft = Part.makeLoft(sections, True, maintain_sections, False).Faces
        wing_shell = Part.Shell(wing_loft)
        wing_solid = Part.Solid(wing_shell)

        # Export
        abs_path = os.path.abspath("{0}{1}_{2}.stp".format(file_tag, airplane_name, self.name))
        wing_solid.exportStep(abs_path)


    def export_dxf(self, airplane_name, **kwargs):
        """Creates a dxf representing successive sections of the wing segment.

        Parameters
        ----------
        file_tag : str, optional
            Optional tag to prepend to output filename default. The output files will be named "<AIRCRAFT_NAME>_<WING_NAME>.stp".

        section_resolution : int, optional
            Number of points to use in discretizing the airfoil section outline. Defaults to 200.
        
        number_guide_curves : int, optional
            Number of guidecurves to create. Defaults to 2 (one at the leading edge, one at the trailing edge).
        
        export_english_units : bool, optional
            Whether to export the dxf file in English units. Defaults to True.

        dxf_line_type : str, optional
            Type of line to be used in the .dxf file creation. Options include 'line', 'spline', and 'polyline'. Defaults to 'spline'.
        
        export_as_prismoid : bool, optional
            Whether to export each airfoil as a rectangle. Forces number_guide_curves to 4 and section_resolution to 5. Defaults to False.
        """

        # Get kwargs
        file_tag = kwargs.get("file_tag", "")
        section_res = kwargs.get("section_resolution", 200)
        number_guide_curves = kwargs.get("number_guide_curves", 2)
        export_english_units = kwargs.get("export_english_units", True)
        dxf_line_type = kwargs.get("dxf_line_type", "spline")
        export_as_prismoid = kwargs.get("export_as_prismoid", False)
        if export_as_prismoid:
            number_guide_curves = 4
            section_res = 5

        # raise error if number of guidecurves is less than 1
        if number_guide_curves < 1:
            raise ValueError("number_guide_curves must be greater than 1")

        # initialize closed trailing edge
        close_te = True

        # determine number of extra dxf 2d sections to create
        two_plus_airfoils = len(self._airfoils) > 1
        if two_plus_airfoils:
            num_2D = len(self._airfoils)
        else:
            num_2D = 2
        
        # # Initialize arrays 2D, and guide curve arrays
        X_GC = np.zeros((number_guide_curves,self.N+1))
        Y_GC = np.zeros((number_guide_curves,self.N+1))
        Z_GC = np.zeros((number_guide_curves,self.N+1))
        X_AF = np.zeros((number_guide_curves * num_2D,),dtype=np.ndarray)
        Y_AF = np.zeros((number_guide_curves * num_2D,),dtype=np.ndarray)
        Z_AF = np.zeros((number_guide_curves * num_2D,),dtype=np.ndarray)

        # initialize counter
        k = 0

        # Initialize 3D arrays
        X_3D = np.zeros((self.N+1, section_res))
        Y_3D = np.zeros((self.N+1, section_res))
        Z_3D = np.zeros((self.N+1, section_res))

        # Initialize DXF line type array
        dxf_line_types = [dxf_line_type] * num_2D

        # set unit multiplier
        if self._unit_sys == "English":
            unit_multiplier = 12.0
        elif self._unit_sys == "SI":
            unit_multiplier = 100.0 / 2.54
        else:
            raise ValueError("{0} not an acceptable unit system, must be 'English' or 'SI'".format(self._unit_sys))
        
        # change unit multiplier if english not desired
        if not export_english_units:
            unit_multiplier /= (100.0 / 2.54)

        # Fill arrays
        for i, s_i in enumerate(self.node_span_locs):

            # Get outline points
            if export_as_prismoid:
                outline = self._get_rectangle_outline_coords_at_span(s_i)
            else:
                outline = self._get_airfoil_outline_coords_at_span(s_i, section_res, close_te)

            # Store in arrays
            X_3D[i,:] = outline[:,0] * unit_multiplier
            Y_3D[i,:] = outline[:,1] * unit_multiplier
            Z_3D[i,:] = outline[:,2] * unit_multiplier

        # initialize guide curve indices
        guide_curve_indices = np.zeros((number_guide_curves,))

        # determine indices for each guide curve point
        for i in range(guide_curve_indices.shape[0]):

            # if not the first index, determine the index to place a guide curve
            if i != 0:
                guide_curve_indices[i] = (float(i) * (number_guide_curves-1) / number_guide_curves) * section_res // (number_guide_curves-1)
        
        # add the guide curve points at each index
        for i in range(guide_curve_indices.shape[0]):

            X_GC[i,:] = X_3D[:,int(guide_curve_indices[i])]
            Y_GC[i,:] = Y_3D[:,int(guide_curve_indices[i])]
            Z_GC[i,:] = Z_3D[:,int(guide_curve_indices[i])]

        # Export guide curves
        folder_path = os.path.abspath("{0}_dxf_files".format(airplane_name))
        file_path = folder_path + "/{0}{1}_{2}_GC".format(file_tag, airplane_name, self.name)
        dxf(file_path, X_GC, Y_GC, Z_GC,geometry="spline")

        # Add section resolution to guide curve indices
        guide_curve_indices = np.append(guide_curve_indices,section_res-1)

        # run through each 2d shape and add to the _2D arrays
        for i in range(num_2D):

            # Returns the airfoil section outline in body-fixed coordinates at the specified span fraction with the specified number of points
            if two_plus_airfoils:
                span = self._airfoil_spans[i]
            else:
                span = float(i)
            
            # determine 3D shape at this location
            if export_as_prismoid:
                outline = self._get_rectangle_outline_coords_at_span(span) * unit_multiplier
            else:
                outline = self._get_airfoil_outline_coords_at_span(span, section_res, close_te) * unit_multiplier
            
            # add 3D outline splines to array
            for j in range(number_guide_curves):
                X_AF[k] = outline[int(guide_curve_indices[j]):int(guide_curve_indices[j+1]+1),0]
                Y_AF[k] = outline[int(guide_curve_indices[j]):int(guide_curve_indices[j+1]+1),1]
                Z_AF[k] = outline[int(guide_curve_indices[j]):int(guide_curve_indices[j+1]+1),2]
                k += 1
        
        # create airfoils DXF file
        file_path = folder_path + "/{0}{1}_{2}_AF".format(file_tag, airplane_name, self.name)
        dxf(file_path, X_AF, Y_AF, Z_AF, geometry="spline")
