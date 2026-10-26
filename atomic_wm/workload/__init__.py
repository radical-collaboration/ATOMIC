"""Synthetic ATOMIC workload tools.

Three console scripts stand in for the real codes of the demo pipeline:

* ``atomic-fake-md``          -- MD simulation      (type ``simulation``)
* ``atomic-fake-train``       -- ML training        (type ``ml_training``)
* ``atomic-fake-descriptors`` -- descriptor analysis(type ``analysis``)

They produce *numbers, not science*, but they produce them in the same
shape a real code would: a JSON envelope with metric series the demo UI
can plot.  Everything here is stdlib only -- the tools run under whatever
python a joined resource happens to provide.
"""
