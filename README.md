# MachUpHydro
An implementation of the Goates-Hunsaker method for solving the general numerical lifting-line problem in the context of hydrofoils, with free surface effects included. 

This method has been developed in recent years based on the original numerical liting-line method developed by Phillips and Snyder. The main reference for the Goates-Hunsaker method is:

C. D. Goates and D. F. Hunsaker, "Practical Implementation of a General Numerical Lifting-Line Theory," *AIAA SciTech Forum*, Virtual Event, 2021.

Further background can be found in the following sources:

W. F. Phillips and D. O. Snyder. "Modern Adaptation of Prandtl's Classic Lifting-Line Theory", *Journal of Aircraft*, Vol. 37, No. 4 (2000), pp. 662-670.

W. F. Phillips, "Flow over Multiple Lifting Surfaces," *Mechanics of Flight*, 2nd ed., Wiley, New Jersey, 2010, pp. 94 -107.

J. T. Reid and D. F. Hunsaker, "A General Approach to Lifting-Line Theory, Applied to Wings with Sweep," *AIAA SciTech Forum*, Orlando, 2020.

## Documentation
Documentation on the original MachUpX can be found at [ReadTheDocs](https://machupx.readthedocs.io). Please refer to the documentation for instructions on installation, etc. Specific help with package functions can also be found in the docstrings.

Note that MachUpHydro currently only supports working in SI units. Imperial units are not supported.

Additional input options are available with MachUpHydro for the "scene" class under the "scene" heading. These are:
- "surface_effect_conditions" : dictionary containing options for controlling how surface effects are handled.
  - "has_free_surface" : boolean, whether or not to include a surface effect boundary condition in the simulation. Defaults to False.
  - "surface_plane_normal" : 3D vector array [x,y,z], specifies the unit vector normal to the surface in 3D cartesian coordinates. Defaults to [0,0,1].
  - "point_on_surface" : 3D vector array [x.y.z], specifies a point on the surface in #D cartesian coordinates. Defaults to [0,0,0].
  - "biplane_BC" : boolean, whether to use the biplane version of surface effect for the simulation.. If false, then ground effect will be simulated instead. Defaults to False.
  - "wave_corrections" : boolean, whether to correct the for the effect of gravity and surface waves. Currently experimental and recommended to keep turned off. Defaults to False.
  - "submergence" : float, the dimensional submergence of the hydrofoil to be used for wave and gravity corrections. Defaults to 0. Only used if "wave_corrections" is set to True.


## Support
There is an active MachUpX discussion forum on [Google Groups](https://groups.google.com/forum/#!categories/machup_forum). Help on using MachUpX can be found there.
For bugs, create a new issue on the Github repo.

## License
This project is licensed under the MIT license. See LICENSE file for more information. 
