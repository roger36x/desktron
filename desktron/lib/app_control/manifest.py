"""`agent_control` v1alpha1 的轻量校验。

正式结构发布在 schemas/agent-control.schema.json。运行时不引入 jsonschema 依赖，
这里校验会影响权限与路由的字段；未知字段仍由 schema/测试负责发现。
"""
import re


PROTOCOL = "desk.app-control/v1alpha1"
LEVELS = {"L1", "L2", "L3", "L4"}
OPERABILITY = {"none"} | LEVELS
EFFECTS = {"read_only", "reversible_write", "external_write", "destructive"}
APPROVALS = {"never", "risk_based", "always"}
IDEMPOTENCY = {"none", "keyed", "required"}
NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")
# 原生投影器由宿主内置实现，manifest 只能从已知实现中选择。
NATIVE_PROJECTORS = {"ax-structural/v1"}
NATIVE_KEYS = {"bundle_id", "target_binding", "code_requirement", "projector",
               "allowed_roles", "allowed_semantics", "allowed_values", "label_values"}


def _named_rows(value, field, errors, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty):
        errors.append("agent_control.%s 必须是%s数组" % (field, "" if allow_empty else "非空"))
        return []
    seen = set()
    rows = []
    for i, row in enumerate(value):
        if not isinstance(row, dict):
            errors.append("agent_control.%s[%d] 必须是对象" % (field, i))
            continue
        name = row.get("name") or row.get("type")
        if not isinstance(name, str) or not name or not NAME_RE.fullmatch(name):
            errors.append("agent_control.%s[%d] 名称非法" % (field, i))
        elif name in seen:
            errors.append("agent_control.%s 名称重复:%s" % (field, name))
        else:
            seen.add(name)
        rows.append(row)
    return rows


def validate_agent_control(control):
    """返回错误列表；缺少 agent_control 由调用方视为未声明，不是错误。"""
    errors = []
    if not isinstance(control, dict):
        return ["agent_control 必须是对象"]
    if control.get("protocol") != PROTOCOL:
        errors.append("agent_control.protocol 仅支持 %s" % PROTOCOL)
    identity = control.get("identity")
    if not isinstance(identity, dict):
        errors.append("agent_control.identity 必须是对象")
    else:
        for key in ("app", "version", "adapter"):
            value = identity.get(key)
            if not isinstance(value, str) or not value.strip() or not NAME_RE.fullmatch(value):
                errors.append("agent_control.identity.%s 缺失或非法" % key)
    if control.get("level") not in LEVELS:
        errors.append("agent_control.level 必须是 L1-L4")
    resources = _named_rows(control.get("resources"), "resources", errors)
    for i, row in enumerate(resources):
        if not isinstance(row.get("scope"), str) or not row.get("scope"):
            errors.append("agent_control.resources[%d].scope 缺失" % i)
    native = control.get("native_application")
    if native is not None:
        if not isinstance(native, dict):
            errors.append("agent_control.native_application 必须是对象")
        else:
            bundle = native.get("bundle_id")
            binding = native.get("target_binding")
            if not isinstance(bundle, str) or not bundle.strip() or bundle == "*":
                errors.append("agent_control.native_application.bundle_id 缺失或非法")
            if binding != ["bundle_id", "pid", "window_id"]:
                errors.append("agent_control.native_application.target_binding 必须精确绑定 bundle_id/pid/window_id")
            requirement = native.get("code_requirement")
            if (not isinstance(requirement, str) or not requirement.strip()
                    or len(requirement) > 1024 or not isinstance(bundle, str)
                    or ('identifier "%s"' % bundle) not in requirement):
                errors.append("agent_control.native_application.code_requirement 必须绑定同一 bundle identifier")
            if native.get("projector") not in NATIVE_PROJECTORS:
                errors.append("agent_control.native_application.projector 仅支持 %s"
                              % "/".join(sorted(NATIVE_PROJECTORS)))
            for key in ("allowed_roles", "allowed_semantics", "allowed_values"):
                value = native.get(key)
                if (not isinstance(value, list) or not value
                        or not all(isinstance(x, str) and x and NAME_RE.fullmatch(x)
                                   for x in value) or len(value) != len(set(value))):
                    errors.append("agent_control.native_application.%s 必须是非空唯一名称数组" % key)
            labels = native.get("label_values", {})
            allowed_values = set(native.get("allowed_values") or []) if isinstance(
                native.get("allowed_values"), list) else set()
            if (not isinstance(labels, dict) or len(labels) > 64
                    or not all(isinstance(k, str) and 0 < len(k.strip()) <= 64
                               and v in allowed_values for k, v in labels.items())):
                errors.append("agent_control.native_application.label_values 只能把固定标签映射到 allowed_values")
            extra = set(native) - NATIVE_KEYS
            if extra:
                errors.append("agent_control.native_application 含未知字段:%s" % ",".join(sorted(extra)))
            if any(str(row.get("type") or "") != "native-window"
                   or str(row.get("scope") or "") != "delegated-application-window"
                   for row in resources):
                errors.append("native_application 资源必须是 delegated-application-window 的 native-window")
    observations = _named_rows(control.get("observations"), "observations", errors)
    for i, row in enumerate(observations):
        if not isinstance(row.get("schema"), str) or not row.get("schema"):
            errors.append("agent_control.observations[%d].schema 缺失" % i)
        if "input_schema" in row and not isinstance(row.get("input_schema"), dict):
            errors.append("agent_control.observations[%d].input_schema 必须是对象" % i)
    if isinstance(native, dict):
        for i, row in enumerate(observations):
            schema = row.get("input_schema")
            if row.get("schema") != "desk.native-window/v1" or (
                    schema is not None and (schema.get("properties") or schema.get("required"))):
                errors.append("原生 observations[%d] 必须是无参数的 desk.native-window/v1" % i)
    actions = _named_rows(control.get("actions"), "actions", errors, allow_empty=True)
    if native is not None and control.get("level") == "L1" and actions:
        errors.append("原生 L1 应用不得声明动作")
    for i, row in enumerate(actions):
        effect = row.get("effect")
        approval = row.get("approval", "risk_based")
        idem = row.get("idempotency")
        timeout = row.get("timeout_ms")
        if effect not in EFFECTS:
            errors.append("agent_control.actions[%d].effect 非法" % i)
        if approval not in APPROVALS:
            errors.append("agent_control.actions[%d].approval 非法" % i)
        if effect in ("external_write", "destructive") and approval != "always":
            errors.append("agent_control.actions[%d] 外部/破坏性动作必须 approval=always" % i)
        if idem not in IDEMPOTENCY:
            errors.append("agent_control.actions[%d].idempotency 非法" % i)
        if effect in ("external_write", "destructive") and idem != "required":
            errors.append("agent_control.actions[%d] 外部/破坏性动作必须要求幂等键" % i)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 100 or timeout > 300000:
            errors.append("agent_control.actions[%d].timeout_ms 应为 100..300000" % i)
        for schema_key in ("input_schema", "output_schema"):
            if schema_key in row and not isinstance(row[schema_key], dict):
                errors.append("agent_control.actions[%d].%s 必须是对象" % (i, schema_key))
    permissions = control.get("permissions")
    expected = {"delegation": "explicit", "lease": "exclusive",
                "takeover": "user_confirmed"}
    if not isinstance(permissions, dict):
        errors.append("agent_control.permissions 必须是对象")
    else:
        for key, value in expected.items():
            if permissions.get(key) != value:
                errors.append("agent_control.permissions.%s 必须是 %s" % (key, value))
        if permissions.get("user_interaction") not in ("pause", "revoke"):
            errors.append("agent_control.permissions.user_interaction 必须是 pause/revoke")
    events = control.get("events")
    if not isinstance(events, list) or not all(isinstance(x, str) for x in events):
        errors.append("agent_control.events 必须是字符串数组")
    receipts = control.get("receipts")
    if not isinstance(receipts, dict) or receipts.get("required") is not True:
        errors.append("agent_control.receipts.required 必须为 true")
    return errors


def validate_app_operability(app, required=False):
    """校验应用对 Agent 可操作性的显式选择。

    存量注册表读取可用 ``required=False`` 保持兼容；任何新注册、更新或包安装都应传
    ``required=True``，避免未声明能力被误当成不可操作，或有控制桥却未进入协议治理。
    """
    if not isinstance(app, dict):
        return ["应用必须是对象"]
    declared = "agent_operability" in app
    if not declared:
        if required:
            return ["agent_operability 缺失，必须明确选择 none 或 L1-L4"]
        # 兼容只针对旧记录缺少选择字段；旧记录若已有控制声明，仍必须校验其安全字段。
        return validate_agent_control(app.get("agent_control")) if "agent_control" in app else []
    level = app.get("agent_operability")
    if level not in OPERABILITY:
        return ["agent_operability 必须是 none 或 L1-L4"]
    control = app.get("agent_control")
    reason = app.get("agent_operability_reason")
    errors = []
    if level == "none":
        if not isinstance(reason, str) or not reason.strip():
            errors.append("agent_operability=none 时必须填写 agent_operability_reason")
        if "agent_control" in app:
            errors.append("agent_operability=none 时不得声明 agent_control")
        return errors
    if not isinstance(control, dict):
        return ["agent_operability=%s 时必须声明 agent_control" % level]
    errors.extend(validate_agent_control(control))
    if control.get("level") != level:
        errors.append("agent_operability 必须与 agent_control.level 一致")
    return errors


def action_spec(control, name):
    for row in (control or {}).get("actions") or []:
        if row.get("name") == name:
            return row
    return None


def validate_value(schema, value, path="input"):
    """动作入参的保守 JSON Schema 子集；不认识的关键字不擅自放宽类型约束。"""
    if not schema:
        return []
    errors = []
    typ = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if typ in checks and not checks[typ](value):
        return ["%s 类型应为 %s" % (path, typ)]
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s 不在允许值中" % path)
    if typ in ("integer", "number") and isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s 小于最小值" % path)
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s 大于最大值" % path)
    if typ == "object" and isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append("%s.%s 缺失" % (path, key))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append("%s.%s 未声明" % (path, key))
        for key, sub in props.items():
            if key in value and isinstance(sub, dict):
                errors.extend(validate_value(sub, value[key], "%s.%s" % (path, key)))
    if typ == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors.extend(validate_value(schema["items"], item, "%s[%d]" % (path, i)))
    return errors
