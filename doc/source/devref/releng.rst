Release Procedure
=================

1. Create a tag for the new release and push it to gerrit
   [#tagging_a_release]_ .
   Infra machinary will take care of the rest and automatically upload
   the release to PyPI.

2. Bump the version in setup.cfg.  This opens the development for
   the next release.

.. [#tagging_a_release] http://docs.openstack.org/infra/manual/creators.html#tagging-a-release
