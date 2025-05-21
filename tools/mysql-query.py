import json
import logging
from typing import Any, Dict, Optional, Tuple, Generator
from contextlib import contextmanager
from sqlalchemy import create_engine, text, pool
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError, OperationalError, ProgrammingError

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

# 全局连接池缓存
ENGINE_CACHE = {}

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (int, float)):
            return float(obj)
        return super().default(obj)

class MySQLQueryTool(Tool):
    def _get_connection_key(self, host: str, port: int, user: str, database: str) -> str:
        """生成连接池缓存的键"""
        return f"{host}:{port}:{user}:{database}"
    
    def _get_engine(self, host: str, port: int, user: str, password: str, database: str) -> Engine:
        """获取或创建数据库引擎（带连接池）"""
        key = self._get_connection_key(host, port, user, database)
        
        if key not in ENGINE_CACHE:
            url_object = URL.create(
                drivername="mysql+pymysql",
                username=user,
                password=password,
                host=host,
                port=int(port),
                database=database
            )
            
            # 配置连接池和超时设置
            engine = create_engine(
                url_object,
                pool_size=5,  # 连接池大小
                max_overflow=10,  # 最大溢出连接数
                pool_timeout=30,  # 连接超时时间
                pool_recycle=3600,  # 连接回收时间（1小时）
                connect_args={
                    "connect_timeout": 10,  # 连接超时设置
                    "read_timeout": 30,     # 读取超时设置
                }
            )
            ENGINE_CACHE[key] = engine
            
        return ENGINE_CACHE[key]
    
    @contextmanager
    def _safe_connection(self, engine: Engine):
        """安全的数据库连接上下文管理器"""
        connection = None
        try:
            connection = engine.connect()
            yield connection
        finally:
            if connection:
                connection.close()
    
    def _execute_query(self, conn, query: str, params: Optional[Dict] = None) -> Tuple[Any, bool]:
        """执行查询，返回结果和是否成功的标志"""
        try:
            sql = text(query)
            result = conn.execute(sql, params or {})
            return result, True
        except (OperationalError, ProgrammingError) as e:
            logging.error(f"SQL执行错误: {str(e)}")
            return str(e), False
        except SQLAlchemyError as e:
            logging.error(f"SQLAlchemy错误: {str(e)}")
            return str(e), False
        except Exception as e:
            logging.exception("未预期的错误")
            return str(e), False
    
    def _is_select_query(self, query: str) -> bool:
        """简单检查查询是否为SELECT查询"""
        return query.strip().upper().startswith("SELECT")
    
    # 使用*invoke方法，符合Dify插件标准
    def _invoke(self, tool_parameters: Dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        """
        执行MySQL查询，并返回分页结果
        Args:
            tool_parameters: 一个包含工具输入参数的字典:
                - host (str): MySQL服务器主机名或IP
                - port (int): MySQL服务器端口号
                - user (str): 数据库用户名
                - password (str): 数据库密码
                - database (str): 数据库名称
                - query (str): 要执行的SQL查询（仅支持SELECT查询）
                - page (int): 分页页码（默认1）
                - pagesize (int): 每页记录数（默认10）
        Yields:
            ToolInvokeMessage: 包含查询结果的消息
        """
        # 从params字典中提取所需参数
        host = tool_parameters.get('host', '')
        port = tool_parameters.get('port', 3306)
        user = tool_parameters.get('user', '')
        password = tool_parameters.get('password', '')
        database = tool_parameters.get('database', '')
        query = tool_parameters.get('query', '')
        page = tool_parameters.get('page', 1)
        pagesize = tool_parameters.get('pagesize', 10)
        
        # 验证参数
        if not query or not query.strip():
            error_message = "查询语句不能为空"
            yield self.create_text_message(error_message)
            return
        
        # 验证分页参数
        try:
            page = max(1, int(page))
            pagesize = max(1, min(100, int(pagesize)))  # 限制每页最大记录数为100
        except (ValueError, TypeError):
            error_message = "分页参数无效"
            yield self.create_text_message(error_message)
            return
        
        try:
            # 获取数据库引擎
            engine = self._get_engine(host, port, user, password, database)
            
            # 检查是否为SELECT查询
            if not self._is_select_query(query):
                error_message = "只支持SELECT查询"
                yield self.create_text_message(error_message)
                return
            
            # 计算分页
            offset = (page - 1) * pagesize
            
            with self._safe_connection(engine) as conn:
                # 优化: 使用SQL_CALC_FOUND_ROWS和FOUND_ROWS()来减少查询次数
                if "SQL_CALC_FOUND_ROWS" not in query.upper():
                    # 对于复杂查询，我们仍然使用两次查询方式
                    # 执行COUNT查询
                    count_query = f"SELECT COUNT(1) FROM ({query}) AS total_count"
                    count_result, success = self._execute_query(conn, count_query)
                    
                    if not success:
                        error_message = f"COUNT查询失败: {count_result}"
                        yield self.create_text_message(error_message)
                        return
                    
                    total = count_result.scalar() or 0
                    
                    # 执行分页查询
                    paginated_query = f"{query} LIMIT {pagesize} OFFSET {offset}"
                    result, success = self._execute_query(conn, paginated_query)
                    
                    if not success:
                        error_message = f"分页查询失败: {result}"
                        yield self.create_text_message(error_message)
                        return
                else:
                    # 对于已包含SQL_CALC_FOUND_ROWS的查询，使用一次查询方式
                    paginated_query = f"{query} LIMIT {pagesize} OFFSET {offset}"
                    result, success = self._execute_query(conn, paginated_query)
                    
                    if not success:
                        error_message = f"查询失败: {result}"
                        yield self.create_text_message(error_message)
                        return
                    
                    # 获取总记录数
                    count_result, success = self._execute_query(conn, "SELECT FOUND_ROWS()")
                    
                    if not success:
                        error_message = f"获取总记录数失败: {count_result}"
                        yield self.create_text_message(error_message)
                        return
                    
                    total = count_result.scalar() or 0
                
                # 处理结果集
                try:
                    # 改进的结果集处理逻辑：更健壮地转换行为字典
                    rows = []
                    if result and hasattr(result, 'keys'):
                        # 获取列名列表
                        column_names = result.keys()
                        
                        # 遍历结果集的每一行
                        for row in result:
                            # 多种方式尝试将行转换为字典
                            try:
                                # 方法1：使用._mapping属性（SQLAlchemy 1.4+）
                                if hasattr(row, '_mapping'):
                                    row_dict = dict(row._mapping)
                                # 方法2：使用索引和列名构建字典
                                else:
                                    row_dict = {}
                                    for idx, column in enumerate(column_names):
                                        try:
                                            row_dict[column] = row[column]
                                        except Exception:
                                            try:
                                                row_dict[column] = row[idx]
                                            except Exception:
                                                row_dict[column] = None
                            except Exception as e:
                                logging.warning(f"行转换错误，尝试替代方法: {str(e)}")
                                # 方法3：使用列表转换
                                row_list = list(row)
                                row_dict = {column_names[i]: val for i, val in enumerate(row_list) if i < len(column_names)}
                            
                            # 确保所有值都是可JSON序列化的
                            for key, value in row_dict.items():
                                if value is not None and not isinstance(value, (str, int, float, bool)):
                                    row_dict[key] = str(value)
                            
                            rows.append(row_dict)
                    else:
                        logging.warning("查询结果为空或不包含键")
                        
                except Exception as e:
                    logging.exception("处理结果集失败")
                    error_message = f"处理结果集失败: {str(e)}"
                    yield self.create_text_message(error_message)
                    return
            
            # 构建结果数据
            result_data = {
                "data": rows,
                "total": total,
                "page": page,
                "pagesize": pagesize
            }
            
            # 生成JSON格式的结果，确保中文等字符正确显示
            result_json = json.dumps(result_data, ensure_ascii=False)
            
            # 返回文本消息
            yield self.create_text_message(result_json)

        except OperationalError as e:
            # 数据库连接错误
            logging.error(f"数据库连接错误: {str(e)}")
            error_message = f"数据库连接失败: {str(e)}"
            yield self.create_text_message(error_message)
        except Exception as e:
            # 其他未预期错误
            logging.exception("MySQL查询工具执行失败")
            error_message = f"查询执行失败: {str(e)}"
            yield self.create_text_message(error_message)
