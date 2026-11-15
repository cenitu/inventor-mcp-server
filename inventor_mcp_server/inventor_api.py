"""Wrapper around Autodesk Inventor COM API.

The wrapper is one class, InventorConnection, put together from the parts below — one
module per area. Reloading this module (the reload_api tool) reloads every part first.
"""
import importlib
import sys

_PARTS = ("api_common", "flat_checks", "api_documents", "api_sketch", "api_features", "api_sheet_metal", "api_views", "api_outputs", "api_library", "api_assembly", "api_tools")


def _part(name):
    """Import a part, or reload it when this module itself is being reloaded."""
    full = f"{__package__}.{name}" if __package__ else name
    loaded = sys.modules.get(full)
    return importlib.reload(loaded) if loaded is not None else importlib.import_module(full)


_parts = {name: _part(name) for name in _PARTS}
globals().update({name: getattr(_parts["api_common"], name) for name in _parts["api_common"].__all__})
flat_pattern_problems = _parts["flat_checks"].flat_pattern_problems
rule_from_name = _parts["flat_checks"].rule_from_name


# A later part wins where two define the same name, as later definitions did in one class.
class InventorConnection(_parts["api_tools"].ToolsMixin,
                         _parts["api_assembly"].AssemblyMixin,
                         _parts["api_library"].LibraryMixin,
                         _parts["api_outputs"].OutputsMixin,
                         _parts["api_views"].ViewsMixin,
                         _parts["api_sheet_metal"].SheetMetalMixin,
                         _parts["api_features"].FeaturesMixin,
                         _parts["api_sketch"].SketchMixin,
                         _parts["api_documents"].DocumentsMixin):
    """One connection to one Inventor: every wrapper method, from all the parts."""


for _name, _class in (("api_documents", "DocumentsMixin"), ("api_sketch", "SketchMixin"), ("api_features", "FeaturesMixin"), ("api_sheet_metal", "SheetMetalMixin"), ("api_views", "ViewsMixin"), ("api_outputs", "OutputsMixin"), ("api_library", "LibraryMixin"), ("api_assembly", "AssemblyMixin"), ("api_tools", "ToolsMixin")):
    assert issubclass(InventorConnection, getattr(_parts[_name], _class))
