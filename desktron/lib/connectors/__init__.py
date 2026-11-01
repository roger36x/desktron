"""接入层(connectors):供应商描述符,与 engines/ 同构。

领域层不出现供应商名字;供应商能力差异用枚举声明,行为经描述符触达。
依赖单向:connectors 不 import 任何领域模块。
"""
