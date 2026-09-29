async def comfy_entrypoint():
    # Keep the package's transport/media modules importable for unit tests and
    # tooling that do not have the ComfyUI runtime on sys.path.
    from . import creative_routes  # noqa: F401
    from . import drama_routes  # noqa: F401 - optional workbench bridge
    from . import profile_routes  # noqa: F401 - registers same-origin routes
    from .node import DGXRemoteH3Extension

    return DGXRemoteH3Extension()


WEB_DIRECTORY = "./web"

__all__ = ["WEB_DIRECTORY", "comfy_entrypoint"]
