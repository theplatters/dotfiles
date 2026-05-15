import gi
gi.require_version('EDataServer', '1.2')
from gi.repository import EDataServer

registry = EDataServer.SourceRegistry.new_sync(None)
sources = registry.list_sources(None)

for source in sources:
    print(f"Source: {source.get_display_name()} ({source.get_uid()})")
    for ext_name in source.list_extensions():
        print(f"  Extension: {ext_name}")
        ext = source.get_extension(ext_name)
        for prop in ext.list_properties():
            try:
                val = ext.get_property(prop.name)
                print(f"    {prop.name}: {val}")
            except:
                pass
