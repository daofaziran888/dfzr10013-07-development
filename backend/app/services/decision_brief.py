"""决策建议与窗口预警的连接服务：把预警分级映射到收益/风险输入，并保留窗口版本。

设计要点：
- 建议生成时固定引用一个“预警分级数据集版本”，并逐条记录引用的窗口编号+窗口版本；
- 推送前重新核对引用：窗口版本变化、预警等级变化、预警被更正而未标记，都会被拦下；
- 映射不覆盖预警原始依据，建议只追加“收益输入/风险输入/处置优先序”条目，
  便于逐项回溯到触发观测。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from typing import Any

from app.services.alert import alert_service

MODULE = "decision_briefs"
BENEFIT_WEIGHT = {"严重": 0.30, "较重": 0.18, "一般": 0.10, "提示": 0.05}
RISK_WEIGHT = {"严重": 0.85, "较重": 0.6, "一般": 0.35, "提示": 0.2}
DISPOSAL_ORDER = ["立即处置", "限时处置", "跟踪观察"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _brief_id(sequence: int) -> str:
    return f"DB-{sequence:04d}"


class DecisionBriefService:
    def __init__(self) -> None:
        self._briefs: list[dict[str, Any]] = []
        self._sequence = 1

    # ---------------------------------------------------------------- 生成
    def generate(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """从分级数据集生成/刷新建议；空数据集不生成“无预警”建议。"""
        snapshot = alert_service.dataset()
        indicator = str(values.get("指标") or "").strip()
        dimension = str(values.get("维度") or "").strip()
        theme = str(values.get("分析主题") or "").strip()
        option = str(values.get("决策选项") or "").strip()
        if not theme:
            theme = f"{indicator or '全指标'}窗口异常处置建议"
        if not option:
            option = "按预警分级限时处置并跟踪缺口补齐"

        if not snapshot["预警"] and not snapshot["待补齐缺口"]:
            return None, "当前分级数据集没有可用预警或缺口，不能生成决策建议"

        rows = self._select_alerts(snapshot, indicator, dimension)
        if indicator and not rows:
            return None, f"指标「{indicator}」在数据集版本 {snapshot['数据集版本']} 中没有触发预警"
        if not rows:
            # 只有缺口没有触发预警时，收益为 0，风险只来自缺口。
            affected = []
        else:
            affected = rows

        stale, problems = self._stale_reference(snapshot)
        if stale:
            return None, "分级数据集引用不自洽，暂不生成建议：" + "；".join(problems)

        benefits: list[dict[str, Any]] = []
        risks: list[dict[str, Any]] = []
        windows: list[dict[str, Any]] = []
        priority_seed: dict[str, float] = {}
        for alert in affected:
            window = self._window(snapshot, alert["窗口编号"])
            if window is None:
                continue
            weight = BENEFIT_WEIGHT.get(alert["预警级别"], 0.1)
            risk_weight = RISK_WEIGHT.get(alert["预警级别"], 0.3)
            ratio = float(alert.get("最大超出比例", 0) or 0)
            benefit_value = round(100 * weight * (1 + min(ratio, 1.0)), 2)
            risk_value = round(100 * risk_weight * (1 + min(ratio, 2.0)), 2)
            gap_count = len(window.get("待补齐缺口", []))
            if gap_count:
                risk_value = round(risk_value + 2.0 * gap_count, 2)
            if any(segment["含缺口"] for segment in alert["触发依据"]["触发连续段"]):
                risks.append({
                    "来源预警": alert["预警编号"],
                    "窗口编号": alert["窗口编号"],
                    "窗口版本": alert["窗口版本"],
                    "风险类型": "缺口可能掩盖超限",
                    "风险值": round(1.5 * gap_count or 1.5, 2),
                    "依据": "触发连续段内含待补齐缺口，缺口不按恢复处理",
                    "来源观测": alert["触发依据"]["超限观测编号"],
                })
            benefits.append({
                "来源预警": alert["预警编号"],
                "窗口编号": alert["窗口编号"],
                "窗口版本": alert["窗口版本"],
                "指标": alert["指标"],
                "维度": alert["维度"],
                "预警级别": alert["预警级别"],
                "收益值": benefit_value,
                "依据": f"按「{alert['预警级别']}」权重 {weight:.2f}、超出比例 {ratio:.2%} 折算",
                "来源观测": copy.deepcopy(alert["触发依据"]["超限观测编号"]),
            })
            risks.append({
                "来源预警": alert["预警编号"],
                "窗口编号": alert["窗口编号"],
                "窗口版本": alert["窗口版本"],
                "风险类型": "处置滞后",
                "风险值": risk_value,
                "依据": f"按「{alert['预警级别']}」风险权重 {risk_weight:.2f}、超出比例 {ratio:.2%} 折算",
                "来源观测": copy.deepcopy(alert["触发依据"]["超限观测编号"]),
            })
            windows.append({
                "窗口编号": alert["窗口编号"],
                "窗口版本": alert["窗口版本"],
                "指标": alert["指标"],
                "维度": alert["维度"],
                "预警级别": alert["预警级别"],
                "预警编号": alert["预警编号"],
                "实际阈值": copy.deepcopy(alert.get("实际阈值", [])),
            })
            key = alert["维度"]
            priority_seed[key] = max(
                priority_seed.get(key, 0.0),
                float(alert["级别序"]) * 10 + min(ratio, 1.0) * 10,
            )

        for gap in snapshot["待补齐缺口"]:
            window = self._window(snapshot, gap["窗口编号"])
            if window is None:
                continue
            if indicator and window["指标"] != indicator:
                continue
            if dimension and window["维度"] != dimension:
                continue
            if any(item["窗口编号"] == gap["窗口编号"] for item in windows):
                continue
            risks.append({
                "来源预警": None,
                "窗口编号": gap["窗口编号"],
                "窗口版本": window["窗口版本"],
                "风险类型": "采集缺口",
                "风险值": 3.0,
                "依据": f"窗口 {gap['期望时间']} 未收到观测，缺报不能自动清除预警",
                "来源观测": [gap["前序观测编号"]] + (
                    [gap["后续观测编号"]] if gap.get("后续观测编号") else []
                ),
            })
            windows.append({
                "窗口编号": window["窗口编号"],
                "窗口版本": window["窗口版本"],
                "指标": window["指标"],
                "维度": window["维度"],
                "预警级别": window["预警级别"],
                "预警编号": None,
                "实际阈值": copy.deepcopy(window["实际阈值"]),
            })

        disposal = []
        for dimension_key, score in sorted(priority_seed.items(), key=lambda item: -item[1]):
            disposal.append({
                "对象": dimension_key,
                "优先序": len(disposal) + 1,
                "优先级得分": round(score, 2),
                "处置时限": (
                    DISPOSAL_ORDER[0] if score >= 35 else
                    DISPOSAL_ORDER[1] if score >= 20 else DISPOSAL_ORDER[2]
                ),
                "关联窗口": [
                    item["窗口编号"] for item in windows if item["维度"] == dimension_key
                ],
            })
        for risk in risks:
            if risk["风险类型"] == "采集缺口" and not any(
                item["对象"] == next(
                    (w["维度"] for w in windows if w["窗口编号"] == risk["窗口编号"]), ""
                )
                for item in disposal
            ):
                dimension_key = next(
                    (w["维度"] for w in windows if w["窗口编号"] == risk["窗口编号"]), ""
                )
                disposal.append({
                    "对象": dimension_key,
                    "优先序": len(disposal) + 1,
                    "优先级得分": 15.0,
                    "处置时限": DISPOSAL_ORDER[2],
                    "关联窗口": [risk["窗口编号"]],
                })
        disposal.sort(key=lambda item: (-item["优先级得分"], item["对象"]))
        for index, item in enumerate(disposal, start=1):
            item["优先序"] = index

        total_benefit = round(sum(item["收益值"] for item in benefits), 2)
        total_risk = round(sum(item["风险值"] for item in risks), 2)
        source_fingerprint = hashlib.sha1(
            repr(
                sorted(
                    (item["窗口编号"], item["窗口版本"], item["预警级别"])
                    for item in windows
                )
            ).encode("utf-8")
        ).hexdigest()

        brief = {
            "id": self._sequence,
            "建议编号": _brief_id(self._sequence),
            "分析主题": theme,
            "决策选项": option,
            "收益评估": f"预计可避免损失 {total_benefit:g}（基于 {len(benefits)} 条预警折算）",
            "风险评估": f"综合风险 {total_risk:g}，其中处置滞后 {len([r for r in risks if r['风险类型'] == '处置滞后'])} 项、采集缺口 {len([r for r in risks if r['风险类型'] == '采集缺口'])} 项",
            "推荐方案": (
                disposal[0]["对象"] + "：" + disposal[0]["处置时限"]
                if disposal
                else "补齐缺口后重新评估"
            ),
            "生成时间": _now(),
            "状态": "已推送",
            "收益输入": benefits,
            "风险输入": risks,
            "处置优先序": disposal,
            "引用数据集版本": snapshot["数据集版本"],
            "引用窗口": windows,
            "引用指纹": source_fingerprint,
            "被更正引用": [],
            "推送核对": [],
            "allowed_actions": [],
        }
        self._briefs.append(brief)
        self._sequence += 1
        return copy.deepcopy(brief), "决策建议已按当前窗口版本生成并推送"

    # ---------------------------------------------------------------- 核对
    def _stale_reference(self, snapshot: dict[str, Any]) -> tuple[bool, list[str]]:
        """确认快照里的每条预警都能找到同版本窗口。"""
        problems: list[str] = []
        window_versions = {
            window["窗口编号"]: window["窗口版本"] for window in snapshot["窗口"]
        }
        for alert in snapshot["预警"]:
            if window_versions.get(alert["窗口编号"]) != alert["窗口版本"]:
                problems.append(
                    f"预警 {alert['预警编号']} 引用窗口版本 {alert['窗口版本']}，"
                    f"数据集窗口版本为 {window_versions.get(alert['窗口编号'])}"
                )
        return bool(problems), problems

    def check(self, brief_id: str | None = None) -> dict[str, Any]:
        """推送前核对：窗口版本、预警级别、更正标记是否与当前数据集一致。"""
        snapshot = alert_service.dataset()
        window_versions = {
            window["窗口编号"]: window for window in snapshot["窗口"]
        }
        alert_map = {alert["预警编号"]: alert for alert in snapshot["预警"]}
        corrected_map = {
            alert["预警编号"]: alert for alert in snapshot["已更正预警"]
        }
        results: list[dict[str, Any]] = []
        briefs = [self._find(brief_id)] if brief_id else copy.deepcopy(self._briefs)
        if brief_id and briefs[0] is None:
            return {"ok": False, "说明": f"决策建议 {brief_id} 不存在", "核对结果": []}
        for brief in briefs:
            issues: list[dict[str, Any]] = []
            for reference in brief["引用窗口"]:
                window = window_versions.get(reference["窗口编号"])
                if window is None:
                    issues.append({
                        "类型": "窗口已不存在",
                        "窗口编号": reference["窗口编号"],
                        "引用窗口版本": reference["窗口版本"],
                        "说明": "引用窗口在当前版本中缺失",
                    })
                    continue
                if int(window["窗口版本"]) != int(reference["窗口版本"]):
                    issues.append({
                        "类型": "窗口版本变化",
                        "窗口编号": reference["窗口编号"],
                        "引用窗口版本": reference["窗口版本"],
                        "当前窗口版本": window["窗口版本"],
                        "说明": "窗口成员或阈值已变化，建议引用的结论已过期",
                    })
                alert = alert_map.get(reference["预警编号"]) if reference["预警编号"] else None
                retired = corrected_map.get(reference["预警编号"]) if reference["预警编号"] else None
                if reference["预警编号"]:
                    if alert is None:
                        issues.append({
                            "类型": "预警已更正",
                            "窗口编号": reference["窗口编号"],
                            "预警编号": reference["预警编号"],
                            "引用级别": reference["预警级别"],
                            "说明": (
                                f"该预警已标记为已更正（原级别 {retired['原预警级别']}），"
                                "建议不能继续使用未标记的旧结论"
                                if retired
                                else "该预警在当前数据集中已不触发"
                            ),
                            "建议标记": True,
                        })
                    elif alert["预警级别"] != reference["预警级别"]:
                        issues.append({
                            "类型": "预警级别变化",
                            "窗口编号": reference["窗口编号"],
                            "预警编号": reference["预警编号"],
                            "引用级别": reference["预警级别"],
                            "当前级别": alert["预警级别"],
                            "说明": "建议的收益/风险输入按旧级别折算，需刷新",
                        })
            if snapshot["数据集版本"] != brief["引用数据集版本"]:
                issues.insert(0, {
                    "类型": "数据集版本变化",
                    "引用数据集版本": brief["引用数据集版本"],
                    "当前数据集版本": snapshot["数据集版本"],
                    "说明": "窗口预警分级数据集已更新，需确认引用是否仍有效",
                    "仅提示": True,
                })
            blocking = [issue for issue in issues if not issue.get("仅提示")]
            results.append({
                "建议编号": brief["建议编号"],
                "引用数据集版本": brief["引用数据集版本"],
                "当前数据集版本": snapshot["数据集版本"],
                "可推送": not blocking,
                "问题": issues,
            })
        return {
            "ok": all(item["可推送"] for item in results),
            "数据集版本": snapshot["数据集版本"],
            "核对结果": results,
        }

    def mark_corrected_usage(
        self, brief_id: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """把“引用到已更正预警”的事实写回建议，避免无痕沿用旧结论。"""
        brief = self._find(brief_id)
        if brief is None:
            return None, f"决策建议 {brief_id} 不存在"
        check = self.check(brief_id)["核对结果"][0]
        corrected = [
            issue for issue in check["问题"]
            if issue["类型"] == "预警已更正"
            and not any(
                marked["预警编号"] == issue["预警编号"]
                for marked in brief["被更正引用"]
            )
        ]
        if not corrected:
            return None, "当前建议没有未标记的已更正预警引用，无需重复标记"
        note = str(values.get("说明") or "").strip()
        for issue in corrected:
            if not any(
                item["预警编号"] == issue["预警编号"]
                for item in brief["被更正引用"]
            ):
                brief["被更正引用"].append({
                    "预警编号": issue["预警编号"],
                    "窗口编号": issue["窗口编号"],
                    "原引用级别": issue["引用级别"],
                    "标记时间": _now(),
                    "说明": note or issue["说明"],
                    "数据集版本": check["当前数据集版本"],
                })
        brief["状态"] = "待刷新"
        return copy.deepcopy(brief), "已把引用到已更正预警的事实写回建议，需刷新后重新推送"

    def push(self, brief_id: str) -> tuple[dict[str, Any] | None, str]:
        brief = self._find(brief_id)
        if brief is None:
            return None, f"决策建议 {brief_id} 不存在"
        check = self.check(brief_id)["核对结果"][0]
        brief["推送核对"] = check["问题"]
        if not check["可推送"]:
            return None, "推送被拦下：" + "；".join(
                issue["说明"] for issue in check["问题"] if not issue.get("仅提示")
            )
        brief["状态"] = "已推送"
        return copy.deepcopy(brief), "决策建议已推送，引用窗口版本与当前数据集一致"

    # ---------------------------------------------------------------- 查询
    def list_briefs(
        self, *, keyword: str | None = None, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        rows = copy.deepcopy(self._briefs)
        if keyword:
            rows = [row for row in rows if keyword in row["建议编号"] or keyword in row["分析主题"]]
        rows.sort(key=lambda item: item["建议编号"], reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_brief(self, brief_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._find(brief_id))

    def _find(self, brief_id: str) -> dict[str, Any] | None:
        return next(
            (brief for brief in self._briefs if brief["建议编号"] == brief_id), None
        )

    def _select_alerts(
        self, snapshot: dict[str, Any], indicator: str, dimension: str
    ) -> list[dict[str, Any]]:
        rows = copy.deepcopy(snapshot["预警"])
        if indicator:
            rows = [row for row in rows if row["指标"] == indicator]
        if dimension:
            rows = [row for row in rows if dimension in row["维度"]]
        rows.sort(key=lambda item: (-item["级别序"], -item["最大超出比例"]))
        return rows

    def _window(self, snapshot: dict[str, Any], window_id: str) -> dict[str, Any] | None:
        return next(
            (window for window in snapshot["窗口"] if window["窗口编号"] == window_id),
            None,
        )


decision_brief_service = DecisionBriefService()
