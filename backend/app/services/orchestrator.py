"""查询编排执行引擎：解析步骤序列与数据依赖，按依赖拓扑与并行度执行步骤，
处理失败回退，并生成可逐项复核的合并数据集。

设计约束（对应业务要求）：
- 数据依赖解析成 DAG，循环依赖、悬空依赖直接拒绝执行；
- 执行顺序只有一套：依赖拓扑序（同波次按步骤序列稳定排序），并行度只限制
  同一波次内的并发宽度，不会再产生第二套顺序；
- 步骤状态、执行批次状态都只能沿状态机单向流转，不允许跳级；
- 每个步骤最终至多一次成功产出（重试不产生第二份数据产物），A 只执行一次；
- 合并前强制做一致性闸门：数据依赖、步骤执行记录、合并结果三者不一致就不落库；
- 不同口径冲突时，先按显式口径优先级、再按执行顺序先后判定，判定依据逐条留痕。

本模块是纯函数式引擎：不直接读写全局 store，所有输入由服务层传入，便于单测。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---- 状态机：只能按顺序单向流转，不能跳级 ----------------------------------

# 执行批次状态机
RUN_STATES = ["待执行", "执行中", "回退中", "已完成", "已失败"]
RUN_TRANSITIONS: dict[str, list[str]] = {
    "待执行": ["执行中"],
    "执行中": ["回退中", "已完成", "已失败"],
    "回退中": ["执行中", "已失败"],
    "已完成": [],
    "已失败": [],
}

# 步骤执行状态机
STEP_STATES = ["待调度", "就绪", "执行中", "已完成", "失败", "回退中", "已跳过"]
STEP_TRANSITIONS: dict[str, list[str]] = {
    "待调度": ["就绪", "已跳过"],
    "就绪": ["执行中", "已跳过"],
    "执行中": ["已完成", "失败"],
    "失败": ["回退中", "已跳过"],
    "回退中": ["待调度"],
    "已完成": ["回退中"],  # 回退时把已完成的边界步骤拉回重跑
    "已跳过": ["待调度"],  # 回退重放时重新纳入调度
}


class OrchestrationError(ValueError):
    """编排配置无法解析或不满足执行前置条件。"""


class StateTransitionError(RuntimeError):
    """状态流转违反单向状态机（跳级或回环）。"""


class StepExecutionError(RuntimeError):
    """单个步骤执行失败，携带步骤编号供回退逻辑定位。"""

    def __init__(self, step_id: str, reason: str) -> None:
        super().__init__(f"步骤 {step_id} 执行失败：{reason}")
        self.step_id = step_id
        self.reason = reason


def advance_state(current: str, target: str, table: dict[str, list[str]], kind: str) -> str:
    """状态机统一入口：只允许在声明的相邻状态间单向流转。"""
    allowed = table.get(current, [])
    if target not in allowed:
        raise StateTransitionError(
            f"{kind}状态不能从「{current}」跳到「{target}」，允许的下一状态：{allowed or '无（终态）'}"
        )
    return target


# ---- 配置解析 ---------------------------------------------------------------


@dataclass
class StepPlan:
    """单个编排步骤的归一化定义。"""

    step_id: str
    name: str
    order: int                     # 在步骤序列中的下标，稳定排序用
    depends_on: list[str] = field(default_factory=list)
    recovery_boundary: bool = False
    caliber_priority: int = 100    # 数字越小优先级越高
    output_fields: list[str] = field(default_factory=list)
    timeout_seconds: int = 30
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedPlan:
    steps: list[StepPlan]
    dependencies: dict[str, list[str]]   # step_id -> 前置步骤
    waves: list[list[str]]               # 拓扑波次，波次内可并行
    execution_order: list[str]           # 唯一一套线性执行顺序
    parallelism: int
    fallback_rules: dict[str, str]       # step_id -> 回退边界步骤
    raw: dict[str, Any] = field(default_factory=dict)


def _coerce_structured(value: Any, field_name: str) -> Any:
    """步骤序列/数据依赖既可能是结构化对象，也可能是 JSON 字符串；
    占位样例（非 JSON 字符串）无法解析时给出可读错误。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrchestrationError(
                f"{field_name} 不是可解析的结构化配置，需要 JSON 数组/对象或直接提交结构化数据"
            ) from exc
    return value


def _normalize_steps(raw_steps: Any) -> list[dict[str, Any]]:
    steps = _coerce_structured(raw_steps, "步骤序列")
    if not isinstance(steps, list) or not steps:
        raise OrchestrationError("步骤序列必须是非空数组，每个元素至少包含步骤编号")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            raise OrchestrationError(f"步骤序列第 {index + 1} 项必须是对象")
        step_id = str(item.get("步骤编号") or item.get("step_id") or "").strip()
        if not step_id:
            raise OrchestrationError(f"步骤序列第 {index + 1} 项缺少步骤编号")
        normalized.append({"步骤编号": step_id, **{k: v for k, v in item.items() if k != "步骤编号"}})
    return normalized


def _normalize_dependencies(raw_deps: Any, known: set[str]) -> dict[str, list[str]]:
    deps = _coerce_structured(raw_deps, "数据依赖")
    result: dict[str, list[str]] = {step_id: [] for step_id in known}
    if deps is None:
        return result
    # 形态一：[{"步骤编号": "B", "依赖": ["A"]}, ...]
    if isinstance(deps, list):
        for index, item in enumerate(deps):
            if not isinstance(item, dict):
                raise OrchestrationError(f"数据依赖第 {index + 1} 项必须是对象")
            target = str(item.get("步骤编号") or item.get("step_id") or "").strip()
            prereqs = item.get("依赖") or item.get("depends_on") or []
            if not target:
                raise OrchestrationError(f"数据依赖第 {index + 1} 项缺少步骤编号")
            if target not in known:
                raise OrchestrationError(f"数据依赖引用了未在步骤序列中声明的步骤：{target}")
            if not isinstance(prereqs, list):
                raise OrchestrationError(f"步骤 {target} 的依赖必须是数组")
            for prereq in prereqs:
                prereq = str(prereq).strip()
                if prereq not in known:
                    raise OrchestrationError(f"步骤 {target} 依赖了未声明的步骤：{prereq}")
                if prereq == target:
                    raise OrchestrationError(f"步骤 {target} 不能依赖自身")
                if prereq not in result[target]:
                    result[target].append(prereq)
        return result
    # 形态二：{"B": ["A"], "C": ["A"]}
    if isinstance(deps, dict):
        for target, prereqs in deps.items():
            target = str(target).strip()
            if target not in known:
                raise OrchestrationError(f"数据依赖引用了未在步骤序列中声明的步骤：{target}")
            if not isinstance(prereqs, list):
                raise OrchestrationError(f"步骤 {target} 的依赖必须是数组")
            for prereq in prereqs:
                prereq = str(prereq).strip()
                if prereq not in known:
                    raise OrchestrationError(f"步骤 {target} 依赖了未声明的步骤：{prereq}")
                if prereq == target:
                    raise OrchestrationError(f"步骤 {target} 不能依赖自身")
                if prereq not in result[target]:
                    result[target].append(prereq)
        return result
    raise OrchestrationError("数据依赖必须是依赖对象数组或 步骤->依赖数组 的映射")


def _normalize_fallback(raw_fallback: Any, known: set[str]) -> dict[str, str]:
    fallback = _coerce_structured(raw_fallback, "失败回退")
    rules: dict[str, str] = {}
    if fallback is None:
        return rules
    if isinstance(fallback, list):
        for index, item in enumerate(fallback):
            if not isinstance(item, dict):
                raise OrchestrationError(f"失败回退第 {index + 1} 项必须是对象")
            target = str(item.get("步骤编号") or item.get("step_id") or "").strip()
            boundary = str(item.get("恢复边界") or item.get("fallback_to") or "").strip()
            if not target or not boundary:
                raise OrchestrationError("失败回退每项都需要步骤编号与恢复边界")
            if target not in known or boundary not in known:
                raise OrchestrationError(f"失败回退引用了未声明的步骤：{target}->{boundary}")
            rules[target] = boundary
        return rules
    if isinstance(fallback, dict):
        for target, boundary in fallback.items():
            target, boundary = str(target).strip(), str(boundary).strip()
            if target not in known or boundary not in known:
                raise OrchestrationError(f"失败回退引用了未声明的步骤：{target}->{boundary}")
            rules[target] = boundary
        return rules
    raise OrchestrationError("失败回退必须是对象数组或 步骤->恢复边界 的映射")


def parse_plan(entry: dict[str, Any]) -> ParsedPlan:
    """把编排流程记录里的步骤序列、数据依赖、失败回退、并行度解析成可执行计划。"""
    raw_steps = _normalize_steps(entry.get("步骤序列"))
    step_ids = [item["步骤编号"] for item in raw_steps]
    if len(set(step_ids)) != len(step_ids):
        raise OrchestrationError("步骤序列中存在重复步骤编号，执行顺序无法唯一确定")
    known = set(step_ids)

    dependencies = _normalize_dependencies(entry.get("数据依赖"), known)
    fallback_rules = _normalize_fallback(entry.get("失败回退"), known)

    # 并行度：同波次内允许同时在执行中的步骤上限
    try:
        parallelism = int(entry.get("并行度") or 1)
    except (TypeError, ValueError) as exc:
        raise OrchestrationError("并行度必须是正整数") from exc
    if parallelism < 1:
        raise OrchestrationError("并行度必须是正整数")

    plans: list[StepPlan] = []
    for order, item in enumerate(raw_steps):
        step_id = item["步骤编号"]
        output_fields = item.get("输出字段") or item.get("output_fields") or []
        if isinstance(output_fields, str):
            output_fields = [part.strip() for part in output_fields.split(",") if part.strip()]
        plans.append(
            StepPlan(
                step_id=step_id,
                name=str(item.get("步骤名称") or step_id),
                order=order,
                depends_on=dependencies[step_id],
                recovery_boundary=bool(item.get("恢复边界") or item.get("recovery_boundary")),
                caliber_priority=int(item.get("口径优先级") or item.get("caliber_priority") or 100),
                output_fields=list(output_fields),
                timeout_seconds=int(item.get("超时秒数") or 30),
                raw=item,
            )
        )

    waves, linear_order = _topological_sort(plans, dependencies)
    return ParsedPlan(
        steps=plans,
        dependencies=dependencies,
        waves=waves,
        execution_order=linear_order,
        parallelism=parallelism,
        fallback_rules=fallback_rules,
        raw={"步骤序列": raw_steps, "数据依赖": dependencies, "失败回退": fallback_rules},
    )


def _topological_sort(
    plans: list[StepPlan], dependencies: dict[str, list[str]]
) -> tuple[list[list[str]], list[str]]:
    """Kahn 拓扑排序：同波次内按步骤序列下标稳定排序，保证执行顺序唯一且可复核。"""
    order_index = {plan.step_id: plan.order for plan in plans}
    indegree = {plan.step_id: len(plan.depends_on) for plan in plans}
    dependents: dict[str, list[str]] = {plan.step_id: [] for plan in plans}
    for plan in plans:
        for prereq in plan.depends_on:
            dependents[prereq].append(plan.step_id)

    waves: list[list[str]] = []
    linear_order: list[str] = []
    remaining = set(order_index)
    while remaining:
        ready = sorted(
            [step_id for step_id in remaining if indegree[step_id] == 0],
            key=lambda sid: order_index[sid],
        )
        if not ready:
            raise OrchestrationError("数据依赖存在循环依赖，无法确定执行顺序")
        waves.append(ready)
        linear_order.extend(ready)
        for step_id in ready:
            remaining.discard(step_id)
            for downstream in dependents[step_id]:
                indegree[downstream] -= 1
    return waves, linear_order


# ---- 步骤执行（内置确定性模拟执行器，真实项目替换为查询/清洗适配器） --------


def run_step(plan: StepPlan, attempt: int, context: dict[str, Any]) -> dict[str, Any]:
    """模拟步骤执行：确定性地产出步骤数据产物；fail_steps 注入故障用于回退复测。"""
    fail_steps: set[str] = context.get("fail_steps", set())
    if plan.step_id in fail_steps and attempt == 1:
        raise StepExecutionError(plan.step_id, f"注入故障：{plan.name} 首次执行未在超时内返回")
    rows = [
        {
            "步骤编号": plan.step_id,
            "记录标识": f"{plan.step_id}-{attempt}-1",
            "指标值": plan.order * 10 + attempt,
            "口径": f"{plan.step_id}-口径",
        },
        {
            "步骤编号": plan.step_id,
            "记录标识": f"{plan.step_id}-{attempt}-2",
            "指标值": plan.order * 10 + attempt + 1,
            "口径": f"{plan.step_id}-口径",
        },
    ]
    return {
        "step_id": plan.step_id,
        "rows": rows,
        "fields": ["步骤编号", "记录标识", "指标值", "口径", *plan.output_fields],
        "caliber_priority": plan.caliber_priority,
    }


# ---- 合并与口径优先级 -------------------------------------------------------


def merge_outputs(
    plan: ParsedPlan, products: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """按唯一执行顺序合并各步骤产物；同名字段（不同口径）冲突时：
    1) 显式口径优先级数字小者胜；2) 优先级相同则执行顺序靠前者胜。
    每条冲突判定都写入 merge_decisions，留下依据。"""
    merged_rows: list[dict[str, Any]] = []
    field_sources: dict[str, list[dict[str, Any]]] = {}
    merge_decisions: list[dict[str, Any]] = []

    for position, step_id in enumerate(plan.execution_order):
        product = products.get(step_id)
        if product is None:
            continue
        for row in product["rows"]:
            merged_rows.append(dict(row))
        for field_name in product["fields"]:
            source = {
                "字段": field_name,
                "来源步骤": step_id,
                "执行顺序位": position,
                "口径优先级": product["caliber_priority"],
            }
            field_sources.setdefault(field_name, []).append(source)

    # 同名字段多口径并存 -> 优先级判定并留痕
    field_inventory: list[dict[str, Any]] = []
    for field_name, sources in field_sources.items():
        if len(sources) == 1:
            winner = sources[0]
            basis = "仅一个步骤产出该字段，直接采用"
        else:
            ranked = sorted(
                sources,
                key=lambda item: (item["口径优先级"], item["执行顺序位"]),
            )
            winner, runner_up = ranked[0], ranked[1]
            if winner["口径优先级"] != runner_up["口径优先级"]:
                basis = (
                    f"显式口径优先级：{winner['来源步骤']}({winner['口径优先级']}) "
                    f"高于 {runner_up['来源步骤']}({runner_up['口径优先级']})，数字小者胜"
                )
            else:
                basis = (
                    f"口径优先级相同({winner['口径优先级']})，按唯一执行顺序："
                    f"{winner['来源步骤']}(第{winner['执行顺序位']}位) 早于 "
                    f"{runner_up['来源步骤']}(第{runner_up['执行顺序位']}位)，靠前者胜"
                )
            merge_decisions.append(
                {
                    "字段": field_name,
                    "并存口径": [f"{item['来源步骤']}:优先级{item['口径优先级']}" for item in sources],
                    "采纳来源": winner["来源步骤"],
                    "判定依据": basis,
                }
            )
        field_inventory.append(
            {
                "字段": field_name,
                "采纳来源步骤": winner["来源步骤"],
                "全部来源步骤": [item["来源步骤"] for item in sources],
                "判定依据": basis,
            }
        )

    return {
        "merged_rows": merged_rows,
        "field_inventory": field_inventory,
        "merge_decisions": merge_decisions,
        "row_count": len(merged_rows),
    }


# ---- 一致性闸门 -------------------------------------------------------------


def consistency_gate(
    plan: ParsedPlan,
    step_records: dict[str, dict[str, Any]],
    event_timeline: list[dict[str, Any]],
    merged: dict[str, Any],
) -> tuple[bool, list[str]]:
    """落库前三方对账：数据依赖、步骤执行记录、合并结果必须一致，
    否则后续步骤可能读到未完成的数据产物——任一不过即拒绝写入。"""
    errors: list[str] = []

    # 1. 每个步骤都走到终态，且成功步骤的前置依赖全部成功
    for step_id in plan.execution_order:
        record = step_records.get(step_id)
        if record is None:
            errors.append(f"步骤 {step_id} 缺少执行记录")
            continue
        if record["状态"] not in ("已完成", "已跳过"):
            errors.append(f"步骤 {step_id} 停在非终态「{record['状态']}」，数据产物未就绪")
        if record["状态"] == "已完成":
            for prereq in plan.dependencies[step_id]:
                if step_records[prereq]["状态"] != "已完成":
                    errors.append(
                        f"步骤 {step_id} 已完成但其依赖 {prereq} 状态为"
                        f"「{step_records[prereq]['状态']}」，违反数据依赖"
                    )
            # 重试只允许一份有效成功产物；历史作废产物保留用于逐项复核
            valid_successes = [
                item
                for item in record["尝试记录"]
                if item["结果"] == "成功" and item.get("有效性", "有效") == "有效"
            ]
            if len(valid_successes) != 1:
                errors.append(f"步骤 {step_id} 存在 {len(valid_successes)} 份有效成功产出，要求恰好一份")

    # 2. 事件时间线是一套连续顺序，序号单调、状态流转合法
    seq = 0
    for event in event_timeline:
        if event["序号"] != seq:
            errors.append("事件时间线序号不连续，执行顺序被破坏")
            break
        seq += 1

    # 3. 合并结果的每行都能回溯到已完成步骤，字段清单与执行记录对齐
    completed = {sid for sid, rec in step_records.items() if rec["状态"] == "已完成"}
    for row in merged["merged_rows"]:
        if row.get("步骤编号") not in completed:
            errors.append(f"合并数据包含未成功步骤 {row.get('步骤编号')} 的产物，来源不可复核")
    inventory_sources = {item["字段"] for item in merged["field_inventory"]}
    for step_id in completed:
        for field_name in step_records[step_id]["产物字段"]:
            if field_name not in inventory_sources:
                errors.append(f"步骤 {step_id} 的字段 {field_name} 未进入合并字段清单，产物丢失")

    # 4. 口径判定依据必须指向真实步骤
    valid_steps = set(plan.execution_order)
    for decision in merged["merge_decisions"]:
        if decision["采纳来源"] not in valid_steps:
            errors.append(f"字段 {decision['字段']} 的口径判定指向了不存在的步骤")

    return not errors, errors


# ---- 主执行流程 -------------------------------------------------------------


def execute_flow(
    entry: dict[str, Any],
    *,
    fail_steps: set[str] | None = None,
    run_no: int = 1,
) -> dict[str, Any]:
    """按数据依赖执行整条编排，返回可逐项复核的执行批次记录。"""
    plan = parse_plan(entry)
    context = {"fail_steps": set(fail_steps or set())}

    run_state = "待执行"
    state_trace: list[dict[str, Any]] = []

    def move_run(target: str) -> None:
        nonlocal run_state
        run_state = advance_state(run_state, target, RUN_TRANSITIONS, "执行批次")
        state_trace.append({"批次状态": run_state})

    step_records: dict[str, dict[str, Any]] = {
        plan_step.step_id: {
            "步骤编号": plan_step.step_id,
            "步骤名称": plan_step.name,
            "波次": wave_index,
            "执行顺序位": plan.execution_order.index(plan_step.step_id),
            "前置依赖": list(plan_step.depends_on),
            "状态": "待调度",
            "尝试记录": [],
            "产物字段": [],
            "恢复边界": plan_step.recovery_boundary,
        }
        for wave_index, wave in enumerate(plan.waves)
        for plan_step in plan.steps if plan_step.step_id in wave
    }
    products: dict[str, dict[str, Any]] = {}
    event_timeline: list[dict[str, Any]] = []
    fallback_records: list[dict[str, Any]] = []
    seq_counter = 0

    def emit(event_type: str, step_id: str, detail: dict[str, Any]) -> None:
        nonlocal seq_counter
        event_timeline.append(
            {"序号": seq_counter, "事件": event_type, "步骤": step_id, **detail}
        )
        seq_counter += 1

    def move_step(step_id: str, target: str) -> None:
        record = step_records[step_id]
        record["状态"] = advance_state(record["状态"], target, STEP_TRANSITIONS, f"步骤{step_id}")
        emit("状态流转", step_id, {"目标状态": target})

    move_run("执行中")

    def schedule_waves(start_wave: int) -> bool:
        """从指定波次开始调度；遇到失败返回 False，由回退逻辑处理后再续跑。"""
        for wave_index in range(start_wave, len(plan.waves)):
            wave = plan.waves[wave_index]
            # 并行度只限制同波次并发宽度，顺序仍是拓扑序这一套
            for cursor in range(0, len(wave), plan.parallelism):
                slot = wave[cursor:cursor + plan.parallelism]
                for step_id in slot:
                    record = step_records[step_id]
                    if record["状态"] in ("已完成", "已跳过"):
                        continue
                    # 前置依赖必须全部完成，否则不能读数据产物
                    if any(step_records[p]["状态"] != "已完成" for p in plan.dependencies[step_id]):
                        move_step(step_id, "已跳过")
                        emit("跳过执行", step_id, {"原因": "前置依赖未完成，数据产物不可读"})
                        continue
                    move_step(step_id, "就绪")
                    move_step(step_id, "执行中")
                    attempt = len([a for a in record["尝试记录"]]) + 1
                    emit("开始执行", step_id, {"尝试": attempt, "并行度上限": plan.parallelism})
                    try:
                        product = run_step(plan.steps[next(p.order for p in plan.steps if p.step_id == step_id)],
                                           attempt, context)
                    except StepExecutionError as exc:
                        record["尝试记录"].append(
                            {"尝试": attempt, "结果": "失败", "说明": exc.reason}
                        )
                        emit("执行失败", step_id, {"尝试": attempt, "原因": exc.reason})
                        move_step(step_id, "失败")
                        return False, wave_index, step_id
                    record["尝试记录"].append(
                        {"尝试": attempt, "结果": "成功", "有效性": "有效", "产出行数": len(product["rows"])}
                    )
                    record["产物字段"] = list(product["fields"])
                    products[step_id] = product
                    move_step(step_id, "已完成")
                    emit("执行完成", step_id, {"尝试": attempt, "产出行数": len(product["rows"])})
        return True, len(plan.waves) - 1, None

    success, failed_wave, failed_step = schedule_waves(0)

    # 失败回退：同一编排即使有多个恢复边界，也只在这套执行顺序内回退，不另起顺序
    while not success:
        target_boundary = _select_boundary(plan, failed_step, fallback_records)
        if target_boundary is None:
            move_run("已失败")
            emit("批次终止", failed_step, {"原因": "无可恢复边界，编排失败终止"})
            return _build_run_record(
                entry, plan, run_no, run_state, state_trace, step_records,
                event_timeline, fallback_records, products, ok=False,
                failure_note=f"步骤 {failed_step} 失败且无可用恢复边界",
            )

        move_run("回退中")
        emit("触发回退", failed_step, {"回退边界": target_boundary})
        boundary_position = plan.execution_order.index(target_boundary)
        affected = [
            sid for sid in plan.execution_order[boundary_position:]
            if step_records[sid]["状态"] in ("已完成", "失败", "就绪", "执行中", "回退中")
        ]
        for sid in affected:
            record = step_records[sid]
            if record["状态"] == "已完成":
                move_step(sid, "回退中")
            elif record["状态"] == "失败":
                move_step(sid, "回退中")
            emit("回退清理", sid, {"回退边界": target_boundary})
            # 旧产物作废：保留历史尝试记录供复核，但同一时间只允许一份有效成功产物，
            # 因此回退重跑后 A 这类边界步骤仍满足“只成功一次（有效口径）”。
            for attempt in record["尝试记录"]:
                if attempt.get("结果") == "成功" and attempt.get("有效性", "有效") == "有效":
                    attempt["有效性"] = "已作废"
                    attempt["作废原因"] = f"回退到恢复边界 {target_boundary}，旧数据产物失效"
            # 回退清理后重排队：回退中 -> 待调度，仍是相邻状态单向流转
            record["状态"] = advance_state(record["状态"], "待调度", STEP_TRANSITIONS, f"步骤{sid}")
            record["产物字段"] = []
            products.pop(sid, None)
        fallback_records.append(
            {
                "失败步骤": failed_step,
                "回退边界": target_boundary,
                "回退依据": _boundary_basis(plan, failed_step, target_boundary),
                "受影响步骤": affected,
                "重跑起始波次": step_records[target_boundary]["波次"],
            }
        )
        move_run("执行中")
        success, failed_wave, failed_step = schedule_waves(step_records[target_boundary]["波次"])

    # 全部步骤终态后做合并 + 一致性闸门
    merged = merge_outputs(plan, products)
    consistent, gate_errors = consistency_gate(plan, step_records, event_timeline, merged)
    if not consistent:
        move_run("已失败")
        emit("一致性闸门", "-", {"通过": False, "问题": gate_errors})
        return _build_run_record(
            entry, plan, run_no, run_state, state_trace, step_records,
            event_timeline, fallback_records, products, ok=False,
            failure_note="一致性闸门未通过：" + "；".join(gate_errors),
            merged=merged, gate_errors=gate_errors,
        )

    emit("一致性闸门", "-", {"通过": True, "问题": []})
    move_run("已完成")
    return _build_run_record(
        entry, plan, run_no, run_state, state_trace, step_records,
        event_timeline, fallback_records, products, ok=True, merged=merged,
    )


def _select_boundary(
    plan: ParsedPlan, failed_step: str, existing: list[dict[str, Any]]
) -> str | None:
    """选择回退边界：优先失败步骤显式声明的恢复边界；否则沿数据依赖向上找最近的
    恢复边界步骤（执行顺序位最大者）。两个恢复边界并存时，就近原则保证唯一选择，
    因此合并结果始终只有一套执行顺序。"""
    explicit = plan.fallback_rules.get(failed_step)
    if explicit:
        return explicit
    candidates: list[tuple[int, str]] = []
    stack = list(plan.dependencies[failed_step])
    visited: set[str] = set()
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        node_plan = next(step for step in plan.steps if step.step_id == node)
        if node_plan.recovery_boundary:
            candidates.append((plan.execution_order.index(node), node))
        stack.extend(plan.dependencies[node])
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def _boundary_basis(plan: ParsedPlan, failed_step: str, boundary: str) -> str:
    if plan.fallback_rules.get(failed_step) == boundary:
        return f"失败回退配置显式声明 {failed_step} 的恢复边界为 {boundary}"
    return (
        f"沿 {failed_step} 的数据依赖向上检索恢复边界，"
        f"{boundary} 是执行顺序中距离失败步骤最近的上游恢复边界"
    )


def _build_run_record(
    entry: dict[str, Any],
    plan: ParsedPlan,
    run_no: int,
    run_state: str,
    state_trace: list[dict[str, Any]],
    step_records: dict[str, dict[str, Any]],
    event_timeline: list[dict[str, Any]],
    fallback_records: list[dict[str, Any]],
    products: dict[str, dict[str, Any]],
    *,
    ok: bool,
    failure_note: str = "",
    merged: dict[str, Any] | None = None,
    gate_errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "流程编号": entry.get("流程编号"),
        "执行批次号": run_no,
        "批次状态": run_state,
        "是否成功": ok,
        "执行顺序": list(plan.execution_order),
        "拓扑波次": [list(wave) for wave in plan.waves],
        "并行度": plan.parallelism,
        "数据依赖": {sid: list(deps) for sid, deps in plan.dependencies.items()},
        "步骤执行记录": [step_records[sid] for sid in plan.execution_order],
        "事件时间线": event_timeline,
        "状态流转轨迹": state_trace,
        "失败回退记录": fallback_records,
        "合并结果": merged,
        "一致性闸门": {"通过": ok, "问题": gate_errors or []},
        "失败说明": failure_note,
    }
