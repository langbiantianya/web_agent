"""生成的 gRPC 桩代码所在的包。

**这个包是生成物，不要手改**——改契约要改 ``proto/webagent/studio/v1/studio.proto``
然后跑 ``python scripts/gen_proto.py`` 重新生成。

为什么包名叫 ``webagent``：它就是 proto 里的 ``package webagent.studio.v1;``。
``protoc`` 生成的 ``studio_pb2_grpc.py`` 内部会写
``from webagent.studio.v1 import studio_pb2 as ...``——目录名与包名对上，
就不用改任何一行生成代码。

放在仓库根而不是 ``app/`` 下面，是因为 **studio 服务与 web_agent 共用这一份**
（主设计 §22.8.3：契约文件在仓库里只有一份）。studio 容器里不装 web_agent 的
代码，只要把这个目录拷进去就能用。
"""
