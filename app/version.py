"""Single source of truth for the app's version number.

Bump this on every release and add a matching entry to CHANGELOG.md --
installer/publish.ps1 reads CHANGELOG.md's top entry to sanity-check that
this file and the changelog agree before publishing (the trading app's own
publish script learned this lesson the hard way once already: a filename
mismatch between the update feed and what's actually served fails
completely silently at the client end, so anything that can be checked at
publish time should be)."""

__version__ = "1.19.0"
