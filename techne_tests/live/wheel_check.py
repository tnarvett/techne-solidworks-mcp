import asyncio, solidworks_mcp, solidworks_mcp.server as s
t = asyncio.run(s.mcp.list_tools())
print('version', solidworks_mcp.__version__, 'tools', len(t))
import comtypes; print('comtypes', comtypes.__version__)
