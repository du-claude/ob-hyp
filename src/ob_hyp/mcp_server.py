"""Local stdio only. Tool descriptions do not claim model compression."""
from . import runtime, hyp


def serve(cfg):
    from mcp.server.fastmcp import FastMCP
    app = FastMCP('OB + Hyp local candidate')

    @app.tool()
    async def hold(content: str, name: str = '') -> dict:
        """原样写入本地 OB 桶；不自动打标、合并或调用模型。"""
        return {'bucket_id': await runtime.write(cfg,content,name or None)}

    @app.tool()
    async def breath(query: str, max_results: int = 5) -> list:
        """显式检索当前本地使用者的 OB 桶；不提供自动浮现。"""
        return await runtime.search(cfg,query,max_results)

    @app.tool()
    def hyp_query(query: str, max_results: int = 5) -> list:
        """查询已由命令行显式导入的原文；保留来源与作者。"""
        return hyp.query(cfg,query,max_results)

    app.run(transport='stdio')
