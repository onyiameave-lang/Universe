"""Forge core APIs with optional lazy-loaded authorization dependencies."""

__all__ = ["Ed25519AuthorizationVerifier"]


def __getattr__(name):
    if name == "Ed25519AuthorizationVerifier":
        from Forge.core.authorization import Ed25519AuthorizationVerifier

        return Ed25519AuthorizationVerifier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
