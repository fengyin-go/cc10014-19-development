"""查询编排执行引擎与服务层测试（标准库 unittest，无需额外依赖）。

运行：cd backend && .venv/bin/python -m unittest app.tests.test_flow_execution -v
"""
from __future__ import annotations

import copy
import unittest

from app import seed as seed_module
from app.services.contract import OWNER_TEAM, contract_service
from app.services.flow import flow_service
from app.services.orchestrator import (
    OrchestrationError,
    StateTransitionError,
    STEP_TRANSITIONS,
    RUN_TRANSITIONS,
    execute_flow,
    parse_plan,
)
from app.store import store


def make_entry(
    steps: list,
    deps: list | dict,
    *,
    parallelism: int = 2,
    fallback: list | dict | None = None,
    flow_no: str = "FLOW-T",
) -> dict:
    return {
        "流程编号": flow_no,
        "步骤序列": steps,
        "数据依赖": deps,
        "失败回退": fallback or [],
        "并行度": parallelism,
    }


STEPS_ABC = [
    {"步骤编号": "A", "步骤名称": "主查询", "输出字段": ["f1"], "恢复边界": True},
    {"步骤编号": "B", "步骤名称": "明细", "输出字段": ["f2"], "恢复边界": True},
    {"步骤编号": "C", "步骤名称": "汇总", "输出字段": ["f3"]},
]
DEPS_ABC = [{"步骤编号": "B", "依赖": ["A"]}, {"步骤编号": "C", "依赖": ["A"]}]


class PlanParsingTests(unittest.TestCase):
    def test_topological_order_unique_for_diamond_dependencies(self):
        plan = parse_plan(make_entry(STEPS_ABC, DEPS_ABC, parallelism=2))
        self.assertEqual(plan.execution_order, ["A", "B", "C"])
        self.assertEqual(plan.waves, [["A"], ["B", "C"]])

    def test_parallelism_does_not_create_second_order(self):
        # 并行度 1 与并行度 2 产出的线性执行顺序必须是同一套
        order_2 = parse_plan(make_entry(STEPS_ABC, DEPS_ABC, parallelism=2)).execution_order
        order_1 = parse_plan(make_entry(STEPS_ABC, DEPS_ABC, parallelism=1)).execution_order
        self.assertEqual(order_1, order_2)

    def test_cyclic_dependency_rejected(self):
        deps = [
            {"步骤编号": "A", "依赖": ["B"]},
            {"步骤编号": "B", "依赖": ["A"]},
        ]
        with self.assertRaises(OrchestrationError):
            parse_plan(make_entry(STEPS_ABC, deps))

    def test_dangling_dependency_rejected(self):
        deps = [{"步骤编号": "B", "依赖": ["X"]}]
        with self.assertRaises(OrchestrationError):
            parse_plan(make_entry(STEPS_ABC, deps))

    def test_duplicate_step_rejected(self):
        steps = [STEPS_ABC[0], dict(STEPS_ABC[0])]
        with self.assertRaises(OrchestrationError):
            parse_plan(make_entry(steps, []))

    def test_unparseable_string_config_rejected(self):
        entry = make_entry("不是JSON的占位文本", [])
        with self.assertRaises(OrchestrationError):
            parse_plan(entry)

    def test_dict_form_dependencies_supported(self):
        plan = parse_plan(make_entry(STEPS_ABC, {"B": ["A"], "C": ["A"]}))
        self.assertEqual(plan.dependencies["B"], ["A"])
        self.assertEqual(plan.dependencies["C"], ["A"])


class ExecutionTests(unittest.TestCase):
    def test_happy_path_a_executes_once_and_order_respected(self):
        run = execute_flow(make_entry(STEPS_ABC, DEPS_ABC, parallelism=2))
        self.assertTrue(run["是否成功"])
        self.assertEqual(run["执行顺序"], ["A", "B", "C"])
        # A 只执行一次：恰好一次成功尝试，没有第二次
        record_a = next(item for item in run["步骤执行记录"] if item["步骤编号"] == "A")
        successes = [a for a in record_a["尝试记录"] if a["结果"] == "成功"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(record_a["状态"], "已完成")
        # 每个步骤都有终态记录且可逐项复核
        for record in run["步骤执行记录"]:
            self.assertIn(record["状态"], ("已完成", "已跳过"))
            self.assertTrue(record["尝试记录"])
        # 事件时间线序号连续
        seq = [event["序号"] for event in run["事件时间线"]]
        self.assertEqual(seq, list(range(len(seq))))

    def test_merged_dataset_traceable_to_steps(self):
        run = execute_flow(make_entry(STEPS_ABC, DEPS_ABC))
        merged = run["合并结果"]
        self.assertEqual(merged["row_count"], 6)  # 3 步 × 每步 2 行
        source_steps = {row["步骤编号"] for row in merged["merged_rows"]}
        self.assertEqual(source_steps, {"A", "B", "C"})
        # 字段清单里每个字段都有来源与判定依据
        for item in merged["field_inventory"]:
            self.assertTrue(item["采纳来源步骤"])
            self.assertTrue(item["判定依据"])

    def test_caliber_priority_decision_recorded(self):
        # B、C 产出同名字段 共享指标，C 的口径优先级更高(数字小)
        steps = [
            {"步骤编号": "A", "输出字段": []},
            {"步骤编号": "B", "输出字段": ["共享指标"], "口径优先级": 100},
            {"步骤编号": "C", "输出字段": ["共享指标"], "口径优先级": 80},
        ]
        run = execute_flow(make_entry(steps, DEPS_ABC))
        decisions = run["合并结果"]["merge_decisions"]
        decision = next(item for item in decisions if item["字段"] == "共享指标")
        self.assertEqual(decision["采纳来源"], "C")
        self.assertIn("80", decision["判定依据"])

    def test_same_priority_resolved_by_execution_order(self):
        steps = [
            {"步骤编号": "A", "输出字段": []},
            {"步骤编号": "B", "输出字段": ["共享指标"], "口径优先级": 100},
            {"步骤编号": "C", "输出字段": ["共享指标"], "口径优先级": 100},
        ]
        run = execute_flow(make_entry(steps, DEPS_ABC))
        decision = next(
            item for item in run["合并结果"]["merge_decisions"] if item["字段"] == "共享指标"
        )
        self.assertEqual(decision["采纳来源"], "B")  # B 在执行顺序上更早
        self.assertIn("执行顺序", decision["判定依据"])

    def test_failure_fallback_replays_from_boundary_and_keeps_single_order(self):
        # B 首次失败，回退到恢复边界 A 重跑；A 最终仍只成功一次
        run = execute_flow(
            make_entry(STEPS_ABC, DEPS_ABC, fallback=[{"步骤编号": "B", "恢复边界": "A"}]),
            fail_steps={"B"},
        )
        self.assertTrue(run["是否成功"], run.get("失败说明"))
        self.assertEqual(run["执行顺序"], ["A", "B", "C"])  # 仍是同一套顺序
        self.assertEqual(len(run["失败回退记录"]), 1)
        fb = run["失败回退记录"][0]
        self.assertEqual(fb["失败步骤"], "B")
        self.assertEqual(fb["回退边界"], "A")
        self.assertIn("A", fb["受影响步骤"])
        record_a = next(item for item in run["步骤执行记录"] if item["步骤编号"] == "A")
        valid_successes = [
            a for a in record_a["尝试记录"]
            if a["结果"] == "成功" and a.get("有效性", "有效") == "有效"
        ]
        self.assertEqual(len(valid_successes), 1)  # 回退重跑也只保留一份有效成功产物
        # 旧产物标记为已作废并留原因，历史可复核但不会被后续步骤读取
        invalidated = [a for a in record_a["尝试记录"] if a.get("有效性") == "已作废"]
        self.assertEqual(len(invalidated), 1)
        self.assertIn("回退", invalidated[0]["作废原因"])
        # 回退依据留痕
        self.assertTrue(fb["回退依据"])

    def test_two_recovery_boundaries_still_single_order(self):
        # 同一编排两个恢复边界（A、B 都是边界），C 失败时沿数据依赖就近回退到 B，
        # 失败回退与并行度同时作用，合并结果仍只有一套执行顺序
        chain_deps = [
            {"步骤编号": "B", "依赖": ["A"]},
            {"步骤编号": "C", "依赖": ["A", "B"]},
        ]
        run = execute_flow(
            make_entry(STEPS_ABC, chain_deps, parallelism=2), fail_steps={"C"}
        )
        self.assertTrue(run["是否成功"], run.get("失败说明"))
        self.assertEqual(run["执行顺序"], ["A", "B", "C"])
        fb = run["失败回退记录"][0]
        self.assertEqual(fb["回退边界"], "B")  # 就近边界（B 比 A 更靠近 C）
        self.assertIn("最近的上游恢复边界", fb["回退依据"])
        # 回退期间批次状态经过 回退中
        states = [trace["批次状态"] for trace in run["状态流转轨迹"]]
        self.assertIn("回退中", states)
        # 即便发生回退，A、B 也只各保留一份有效成功产物，合并仍按同一套顺序
        for sid in ("A", "B"):
            record = next(item for item in run["步骤执行记录"] if item["步骤编号"] == sid)
            valid = [a for a in record["尝试记录"] if a["结果"] == "成功" and a.get("有效性", "有效") == "有效"]
            self.assertEqual(len(valid), 1)

    def test_no_boundary_fails_run(self):
        steps = [
            {"步骤编号": "A", "输出字段": []},
            {"步骤编号": "B", "输出字段": ["f"]},
        ]
        run = execute_flow(
            make_entry(steps, [{"步骤编号": "B", "依赖": ["A"]}], fallback=[]),
            fail_steps={"B"},
        )
        self.assertFalse(run["是否成功"])
        self.assertEqual(run["批次状态"], "已失败")


class StateMachineTests(unittest.TestCase):
    def test_run_state_machine_no_skipping(self):
        # 待执行不能直接跳到已完成
        with self.assertRaises(StateTransitionError):
            from app.services.orchestrator import advance_state

            advance_state("待执行", "已完成", RUN_TRANSITIONS, "执行批次")
        # 终态不能再流转
        self.assertEqual(RUN_TRANSITIONS["已完成"], [])
        self.assertEqual(RUN_TRANSITIONS["已失败"], [])

    def test_step_state_machine_no_skipping(self):
        with self.assertRaises(StateTransitionError):
            from app.services.orchestrator import advance_state

            advance_state("待调度", "执行中", STEP_TRANSITIONS, "步骤")


class FlowServiceTests(unittest.TestCase):
    def setUp(self):
        # 每个用例从种子源重建内存仓库（深拷贝），彻底隔离嵌套步骤序列的串改；
        # 同时清空执行批次与契约表。
        store._tables = {
            name: [copy.deepcopy(row) for row in rows]
            for name, rows in seed_module.SEED_ROWS.items()
        }
        store._tables["flow_run"] = []
        store._tables["flow_contract"] = []
        self.flow_id_2 = 2  # FLOW-0002 种子数据

    def test_flow_status_transitions_are_adjacent_only(self):
        # FLOW-0002 种子已是已启用；直接启用（跳过待校验）应被拒绝
        # 用 FLOW-0001（草稿）验证跳级
        entry, message = flow_service.run_action(1, "启用流程")
        self.assertIsNone(entry)
        self.assertIn("不能从「草稿」跳到「已启用」", message)
        # 合法相邻流转
        entry, message = flow_service.run_action(1, "提交校验")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "待校验")
        entry, _ = flow_service.run_action(1, "启用流程")
        self.assertEqual(entry["status"], "已启用")

    def test_execute_flow_0002_writes_run_and_contract(self):
        run, message = flow_service.execute(self.flow_id_2)
        self.assertIsNotNone(run, message)
        self.assertTrue(run["是否成功"], message)
        self.assertEqual(run["执行顺序"], ["A", "B", "C"])
        # 契约已写入且内容与执行顺序一致
        contracts = contract_service.list_contracts("FLOW-0002")
        self.assertEqual(len(contracts), 1)
        contract = contracts[0]
        self.assertEqual(contract["执行顺序"], ["A", "B", "C"])
        self.assertTrue(contract["只读"])
        # 转换模块读到的字段清单与合并结果字段一致
        merged_fields = {item["字段"] for item in run["合并结果"]["field_inventory"]}
        contract_fields = {item["字段"] for item in contract["字段清单"]}
        self.assertEqual(merged_fields, contract_fields)
        # 编排记录上的结果合并也回写了同一套顺序
        entry = flow_service.get_entry(self.flow_id_2)
        self.assertEqual(entry["结果合并"]["执行顺序"], ["A", "B", "C"])

    def test_execute_rejected_for_non_enabled_flow(self):
        run, message = flow_service.execute(1)  # FLOW-0001 草稿
        self.assertIsNone(run)
        self.assertIn("已启用", message)

    def test_non_owner_team_cannot_execute_or_reorder(self):
        run, message = flow_service.execute(self.flow_id_2, team="其他团队")
        self.assertIsNone(run)
        self.assertIn("无权", message)
        entry, message = flow_service.reorder_steps(
            self.flow_id_2, ["A", "C", "B"], team="其他团队"
        )
        self.assertIsNone(entry)
        self.assertIn("无权", message)

    def test_owner_can_reorder_within_dependency_constraints(self):
        # B、C 同波次互换不破坏依赖
        entry, message = flow_service.reorder_steps(
            self.flow_id_2, ["A", "C", "B"], team=OWNER_TEAM
        )
        self.assertIsNotNone(entry, message)
        plan = parse_plan(entry)
        self.assertEqual(plan.execution_order, ["A", "C", "B"])

    def test_reorder_that_breaks_dependency_is_rolled_back(self):
        # 把 A 排到最后（下游 B、C 提到前置依赖 A 之前）属于跨拓扑波次调整，
        # 即便仍是合法 DAG 也必须拒绝并恢复原顺序
        original = [step.step_id for step in parse_plan(
            flow_service.get_entry(self.flow_id_2)
        ).steps]
        entry, message = flow_service.reorder_steps(
            self.flow_id_2, ["B", "C", "A"], team=OWNER_TEAM
        )
        self.assertIsNone(entry)
        self.assertIn("前置依赖", message)
        restored = [step.step_id for step in parse_plan(
            flow_service.get_entry(self.flow_id_2)
        ).steps]
        self.assertEqual(restored, original)
        # 同波次内 B、C 互换是允许的（不跨波次、不破坏依赖）
        ok_entry, ok_message = flow_service.reorder_steps(
            self.flow_id_2, ["A", "C", "B"], team=OWNER_TEAM
        )
        self.assertIsNotNone(ok_entry, ok_message)

    def test_contract_write_blocked_for_non_owner(self):
        run, _ = flow_service.execute(self.flow_id_2)
        contract, message = contract_service.write_contract(run, team="其他团队")
        self.assertIsNone(contract)
        self.assertIn("无权", message)

    def test_failed_run_does_not_write_contract(self):
        # 让 C 失败且无回退边界（先去掉 C 的边界可达性：C 没有声明边界且上游无边界）
        entry = flow_service.get_entry(self.flow_id_2)
        entry["失败回退"] = []
        for step in entry["步骤序列"]:
            step.pop("恢复边界", None)
        run, message = flow_service.execute(self.flow_id_2, fail_steps=["C"])
        self.assertFalse(run["是否成功"])
        self.assertEqual(contract_service.list_contracts("FLOW-0002"), [])

    def test_runs_list_and_detail_reviewable(self):
        # 显式确认种子编排带恢复边界，失败回退才有落点
        entry = flow_service.get_entry(self.flow_id_2)
        self.assertTrue(entry["失败回退"])
        flow_service.execute(self.flow_id_2)
        flow_service.execute(self.flow_id_2, fail_steps=["B"])
        rows, message = flow_service.list_runs(self.flow_id_2)
        self.assertIsNotNone(rows, message)
        self.assertEqual([row["执行批次号"] for row in rows], [1, 2])
        detail = flow_service.get_run(self.flow_id_2, 2)
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail["失败回退记录"]), 1)
        # 两个批次都可逐项复核：事件时间线、步骤记录、合并结果齐备
        for row in rows:
            self.assertTrue(row["事件时间线"])
            self.assertTrue(row["步骤执行记录"])
            if row["是否成功"]:
                self.assertIsNotNone(row["合并结果"])


if __name__ == "__main__":
    unittest.main()
