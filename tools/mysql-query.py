import json
import logging
from typing import Any, Dict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from dify_plugin import Tool

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (int, float)):
            return float(obj)
        return super().default(obj)

class MySQLQueryTool(Tool):
    def _invoke(self,
                host: str,
                port: int,
                user: str,
                password: str,
                database: str,
                query: str,
                page: int = 1,
                pagesize: int = 10,
                **kwargs) -> Dict[str, Any]:
        """
执行MySQL查询，并返回分页结果
        """
        try:
            url_object = URL.create(
                drivername="mysql+pymysql",
                username=user,
                password=password,
                host=host,
                port=int(port),
                database=database
            )
            engine = create_engine(url_object)

            with engine.connect() as conn:
                count_result = conn.execute(text(f"SELECT COUNT(1) FROM ({query}) AS total_count"))
                total = count_result.scalar()

                offset = (page - 1) * pagesize
                paginated_query = f"{query} LIMIT {pagesize} OFFSET {offset}"
                result = conn.execute(text(paginated_query))
                rows = [dict(row) for row in result]

            return {
                "data": rows,
                "total": total,
                "page": page,
                "pagesize": pagesize
            }

        except Exception as e:
            logging.exception("MySQL查询工具执行失败")
            return {
                "data": [],
                "total": 0,
                "page": page,
                "pagesize": pagesize
            }