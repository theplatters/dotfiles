import gi
gi.require_version('EDataServer', '1.2')
from gi.repository import EDataServer

registry = EDataServer.SourceRegistry.new_sync(None)
sources = registry.list_sources(None)

known_extensions = [
    EDataServer.SOURCE_EXTENSION_MAIL_ACCOUNT,
    EDataServer.SOURCE_EXTENSION_MAIL_IDENTITY,
    EDataServer.SOURCE_EXTENSION_MAIL_TRANSPORT,
    EDataServer.SOURCE_EXTENSION_CALENDAR,
    EDataServer.SOURCE_EXTENSION_MEMO_LIST,
    EDataServer.SOURCE_EXTENSION_TASK_LIST,
    EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK
]

for source in sources:
    has_ext = False
    for ext_name in known_extensions:
        if source.has_extension(ext_name):
            if not has_ext:
                print(f"Source: {source.get_display_name()} ({source.get_uid()})")
                has_ext = True
            print(f"  Extension: {ext_name}")
            ext = source.get_extension(ext_name)
            for prop in ext.list_properties():
                try:
                    val = ext.get_property(prop.name)
                    print(f"    {prop.name}: {val}")
                except:
                    pass
