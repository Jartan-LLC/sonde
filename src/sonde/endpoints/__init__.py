"""Concrete endpoint implementations. Importing this package registers them all.

To add a new endpoint: drop a module in here that defines an Endpoint subclass
decorated with @register (and, if it's a new API, a Provider in ../provider.py),
then import it below so it registers on package load.
"""

from sonde.endpoints import asset_owners, github_stargazers

__all__ = ["asset_owners", "github_stargazers"]
