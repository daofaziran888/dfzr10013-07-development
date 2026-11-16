"""窗口预警业务规则：观测去重/更正、按窗口聚合、阈值版本解析与分级。

口径约定（与页面说明一致）：
- 窗口成员按“事件时间”归窗，连续超限按事件时间排序计算；
- 每条观测使用其事件时刻实际生效的阈值版本，窗口跨越阈值调整时逐条保留实际阈值；
- 接收顺序只影响入库与更正审计，不改变窗口内的事件时间顺序；
- 内部缺报记为“待补齐缺口”，缺口不视为恢复正常，也不打断连续超限段；
- 相同观测编号 + 相同内容只生效一次；内容变化按“迟到更正”处理并留痕；
- 一次计算先在内存中完整构建，再整体替换数据集；失败不会留下新旧混合结果。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store
from app.seed_alert import (
    ALERT_CORRECTIONS,
    ALERT_OBSERVATIONS,
    ALERT_RULES,
    ALERT_WINDOW_CONFIGS,
)

LEVEL_SEQUENCE = ["提示", "一般", "较重", "严重", "紧急"]
OPEN_STATUSES = ["待处置", "已确认", "已处理"]
TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M")
OBSERVATION_FIELDS = ["观测编号", "指标", "维度", "事件时间", "接收时间", "数值"]


def parse_time(value: Any) -> datetime:
    """解析事件时间/接收时间；空值和非标准格式都给出可读错误。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("时间不能为空")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"时间「{text}」不是 YYYY-MM-DDTHH:MM:SS 格式")
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def iso_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def as_number(value: Any, field: str = "数值") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field}「{value}」不是有效数字")
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{field}「{value}」不是有限数字")
    return number


def fingerprint(indicator: str, dimension: str, event_time: str, value: float) -> str:
    raw = json.dumps(
        {"指标": indicator, "维度": dimension, "事件时间": event_time, "数值": value},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def content_signature(payload: Any) -> str:
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def scope_matches(scope: str, dimension: str) -> bool:
    """适用范围支持“全部区域”或“区域=华东;渠道=线上”，维度按同样格式书写。"""
    text = str(scope or "").strip()
    if not text or text in {"全部", "全部区域", "全区域"}:
        return True
    dimension_parts = {part.strip() for part in str(dimension or "").replace("；", ";").split(";") if part.strip()}
    tokens = [part.strip() for part in text.replace("；", ";").replace("、", ";").split(";") if part.strip()]
    if not tokens:
        return True
    return all(token in dimension_parts for token in tokens)


def exceed_ratio(direction: str, value: float, threshold: float) -> float:
    if direction == "lt":
        return (threshold - value) / threshold if threshold else 0.0
    return (value - threshold) / threshold if threshold else 0.0


def is_exceed(direction: str, value: float, threshold: float) -> bool:
    # 等于阈值不算超限：临界值必须按“严格大于/严格小于”判断。
    return value < threshold if direction == "lt" else value > threshold


def window_id_for(indicator: str, dimension: str, start: datetime) -> str:
    digest = hashlib.sha1(f"{indicator}|{dimension}|{iso_time(start)}".encode("utf-8")).hexdigest()
    return f"WIN-{digest[:10].upper()}"


def alert_id_for(window_id: str, rule_no: str) -> str:
    digest = hashlib.sha1(f"{window_id}|{rule_no}".encode("utf-8")).hexdigest()
    return f"WA-{digest[:10].upper()}"


def level_rank(level_name: str, standard: list[dict[str, Any]], fallback_index: int = 0) -> int:
    if level_name in LEVEL_SEQUENCE:
        return LEVEL_SEQUENCE.index(level_name)
    return len(LEVEL_SEQUENCE) + fallback_index


def normalize_rule(values: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化一条阈值规则版本。"""
    rule_no = str(values.get("规则编号") or "").strip()
    if not rule_no:
        raise ValueError("缺少规则编号")
    indicator = str(values.get("指标") or "").strip()
    if not indicator:
        raise ValueError("缺少指标")
    try:
        version = int(values.get("规则版本") or 1)
    except (TypeError, ValueError):
        raise ValueError("规则版本必须是整数")
    if version < 1:
        raise ValueError("规则版本必须大于 0")
    direction = str(values.get("方向") or "gt").strip().lower()
    if direction not in {"gt", "lt"}:
        raise ValueError("方向只支持 gt（超上限）或 lt（低于下限）")
    threshold = as_number(values.get("阈值上限"), "阈值上限")
    if threshold <= 0:
        raise ValueError("阈值上限必须大于 0")
    try:
        required = int(values.get("连续超限次数") or 1)
    except (TypeError, ValueError):
        raise ValueError("连续超限次数必须是整数")
    if required < 1:
        raise ValueError("连续超限次数必须至少为 1")
    interval_raw = values.get("生效区间") or {}
    if not isinstance(interval_raw, dict):
        raise ValueError("生效区间需写成 {开始, 结束}")
    start = parse_time(interval_raw.get("开始"))
    end_raw = interval_raw.get("结束")
    end = parse_time(end_raw) if end_raw else None
    if end is not None and end <= start:
        raise ValueError("生效区间的结束时间必须晚于开始时间")
    standard_raw = values.get("分级标准") or []
    if not isinstance(standard_raw, list) or not standard_raw:
        raise ValueError("分级标准不能为空")
    standard: list[dict[str, Any]] = []
    for index, item in enumerate(standard_raw):
        if not isinstance(item, dict):
            raise ValueError("分级标准每项需包含级别与最小超出比例")
        name = str(item.get("级别") or "").strip()
        if not name:
            raise ValueError("分级标准缺少级别名称")
        ratio = as_number(item.get("最小超出比例", 0), "最小超出比例")
        if ratio < 0:
            raise ValueError("最小超出比例不能为负数")
        standard.append({"级别": name, "最小超出比例": ratio})
    standard.sort(key=lambda item: item["最小超出比例"])
    return {
        "规则编号": rule_no,
        "规则版本": version,
        "规则名称": str(values.get("规则名称") or rule_no).strip(),
        "指标": indicator,
        "适用范围": str(values.get("适用范围") or "全部区域").strip(),
        "方向": direction,
        "生效条件": str(
            values.get("生效条件")
            or ("数值 > 阈值上限" if direction == "gt" else "数值 < 阈值上限")
        ).strip(),
        "阈值上限": threshold,
        "连续超限次数": required,
        "生效区间": {"开始": iso_time(start), "结束": iso_time(end) if end else None},
        "分级标准": standard,
    }


class WindowAlertService:
    """窗口预警仓储 + 计算引擎。

    观测、规则、数据集都放在这个服务内，store 仍只承载原有的“预警记录”；
    决策建议模块通过同一实例读取分级数据集，保证跨模块使用的是同一版本。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._configs: list[dict[str, Any]] = copy.deepcopy(ALERT_WINDOW_CONFIGS)
        self._rules: list[dict[str, Any]] = [normalize_rule(item) for item in ALERT_RULES]
        self._observations: list[dict[str, Any]] = []
        self._corrections: list[dict[str, Any]] = []
        self._retired_alerts: list[dict[str, Any]] = []
        self._next_observation_id = 1
        self._next_correction_id = 1
        self._dataset: dict[str, Any] = self._empty_dataset()
        self._bootstrap()

    # ---------------------------------------------------------------- 初始化
    def _empty_dataset(self) -> dict[str, Any]:
        return {
            "数据集版本": 0,
            "生成时间": None,
            "观测数": 0,
            "重复观测忽略数": 0,
            "窗口数": 0,
            "预警数": 0,
            "级别统计": {},
            "窗口": [],
            "预警": [],
            "已更正预警": [],
            "待补齐缺口": [],
            "阈值未覆盖观测": [],
        }

    def _bootstrap(self) -> None:
        rows = sorted(
            [*copy.deepcopy(ALERT_OBSERVATIONS), *copy.deepcopy(ALERT_CORRECTIONS)],
            key=lambda row: str(row.get("接收时间", "")),
        )
        for row in rows:
            try:
                self.ingest(row, bootstrap=True)
            except ValueError:
                # 示例数据中的单条坏数据不应让服务起不来；真实接入走接口返回值。
                continue
        # 示例数据按接收顺序逐条回放，会经过多个中间版本；对外从版本 1 起算。
        if self._dataset["数据集版本"] > 0:
            self._dataset["数据集版本"] = 1
            for alert in self._dataset["预警"]:
                alert["数据集版本"] = 1
            for correction in self._corrections:
                correction["数据集版本"] = 1

    # ---------------------------------------------------------------- 规则读取
    def list_rules(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = sorted(
                copy.deepcopy(self._rules),
                key=lambda item: (item["指标"], item["规则编号"], item["规则版本"]),
            )
            # 给出“实际生效至”，便于解释跨越阈值调整的窗口。
            for item in rows:
                same = [r for r in rows if r["规则编号"] == item["规则编号"]]
                later = [r for r in same if r["规则版本"] > item["规则版本"]]
                item["实际生效至"] = later[0]["生效区间"]["开始"] if later else item["生效区间"]["结束"]
            return rows

    def add_rule(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记新的规则版本；版本号唯一，生效区间允许覆盖，按“最晚开始”解析。"""
        try:
            rule = normalize_rule(values)
        except ValueError as error:
            return None, str(error)
        with self._lock:
            if any(
                item["规则编号"] == rule["规则编号"] and item["规则版本"] == rule["规则版本"]
                for item in self._rules
            ):
                return None, f"规则 {rule['规则编号']} 的版本 {rule['规则版本']} 已存在，请使用新版本号"
            same_no = [item for item in self._rules if item["规则编号"] == rule["规则编号"]]
            if same_no and any(item["指标"] != rule["指标"] for item in same_no):
                return None, "同一规则编号不能跨指标定义"
            candidate_rules = self._rules + [rule]
            try:
                computed = self._compute_all(self._observations, candidate_rules)
            except ValueError as error:
                return None, f"规则登记后计算失败：{error}"
            self._rules = candidate_rules
            self._commit_dataset(computed, reason="规则版本登记")
            return copy.deepcopy(rule), "阈值规则版本已登记，窗口分级已按新规则重算"

    # ---------------------------------------------------------------- 观测接入
    def list_observations(self, *, observation_no: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = copy.deepcopy(self._observations)
        if observation_no:
            rows = [row for row in rows if observation_no in row["观测编号"]]
        rows.sort(key=lambda item: (item["接收时间"], item["观测编号"], item["版本"]))
        return rows

    def ingest(self, values: dict[str, Any], *, bootstrap: bool = False) -> dict[str, Any]:
        """登记一条观测。返回新增/重复/更正三种结果，重复不改变数据集。"""
        missing = [field for field in OBSERVATION_FIELDS if values.get(field) in (None, "")]
        if missing:
            raise ValueError(f"缺少必填字段：{'、'.join(missing)}")
        observation_no = str(values["观测编号"]).strip()
        indicator = str(values["指标"]).strip()
        dimension = str(values["维度"]).strip()
        event = parse_time(values["事件时间"])
        received = parse_time(values["接收时间"])
        value = as_number(values["数值"])
        config = next((cfg for cfg in self._configs if cfg["指标"] == indicator), None)
        if config is None:
            raise ValueError(f"指标「{indicator}」尚未配置窗口长度与采集间隔，无法归窗")
        event_text = iso_time(event)
        received_text = iso_time(received)
        mark = fingerprint(indicator, dimension, event_text, value)

        with self._lock:
            same_no = [row for row in self._observations if row["观测编号"] == observation_no]
            active = next((row for row in same_no if row["状态"] == "生效"), None)
            if active is not None and active["内容指纹"] == mark:
                active["重复次数"] = int(active.get("重复次数", 0)) + 1
                active["最后重复接收时间"] = received_text
                self._dataset["重复观测忽略数"] = sum(
                    int(row.get("重复次数", 0)) for row in self._observations
                )
                return {
                    "ok": True,
                    "结果": "重复忽略",
                    "观测编号": observation_no,
                    "数据集版本": self._dataset["数据集版本"],
                    "说明": "同一观测编号与内容已生效，本次重复未再次计入窗口，也不新增预警",
                    "观测": copy.deepcopy(active),
                }
            if active is not None and indicator != active["指标"]:
                raise ValueError(
                    f"观测 {observation_no} 已按指标「{active['指标']}」登记，更正不能改变指标"
                )
            is_correction = active is not None
            previous = copy.deepcopy(active) if active else None
            new_row = {
                "id": self._next_observation_id,
                "观测编号": observation_no,
                "指标": indicator,
                "维度": dimension,
                "事件时间": event_text,
                "接收时间": received_text,
                "数值": value,
                "状态": "生效",
                "版本": (int(active["版本"]) + 1) if active else 1,
                "内容指纹": mark,
                "重复次数": 0,
                "首次接收时间": active["首次接收时间"] if active else received_text,
                "备注": str(values.get("备注") or "").strip(),
                "更正依据": str(values.get("更正依据") or values.get("更正说明") or "").strip(),
            }
            candidate = copy.deepcopy(self._observations)
            if active is not None:
                for row in candidate:
                    if row["id"] == active["id"]:
                        row["状态"] = "已更正"
                        row["被更正时间"] = received_text
                        row["被更正为版本"] = new_row["版本"]
                        break
            else:
                self._next_observation_id = max(self._next_observation_id + 1, new_row["id"] + 1)
            candidate.append(new_row)

            try:
                computed = self._compute_all(candidate, self._rules)
            except ValueError as error:
                raise ValueError(f"观测登记后计算失败，原数据集保持不变：{error}")

            correction_entry: dict[str, Any] | None = None
            if is_correction and previous is not None:
                correction_entry = self._prepare_correction_entry(
                    previous, new_row, computed, values
                )
            prior_dataset = self._dataset
            self._observations = candidate
            self._next_observation_id = max(
                self._next_observation_id,
                max((row["id"] for row in candidate), default=0) + 1,
            )
            self._commit_dataset(
                computed,
                reason="迟到更正" if is_correction else "观测接入",
                correction=correction_entry,
                prior_dataset=prior_dataset,
            )
            committed = self._dataset
            result: dict[str, Any] = {
                "ok": True,
                "结果": "更正" if is_correction else "新增",
                "观测编号": observation_no,
                "观测版本": new_row["版本"],
                "数据集版本": committed["数据集版本"],
                "窗口版本": [
                    {"窗口编号": win["窗口编号"], "窗口版本": win["窗口版本"]}
                    for win in committed["窗口"]
                    if any(
                        member["观测编号"] == observation_no
                        for member in win["窗口成员"]
                    )
                ],
                "说明": (
                    "观测内容与已生效版本不同，已按迟到更正处理并保留原版本"
                    if is_correction
                    else "观测已计入窗口，分级数据集已重算"
                ),
                "当日预警": [
                    {
                        "预警编号": alert["预警编号"],
                        "窗口编号": alert["窗口编号"],
                        "规则编号": alert["规则编号"],
                        "预警级别": alert["预警级别"],
                        "状态": alert["状态"],
                    }
                    for alert in committed["预警"]
                    if alert["窗口编号"]
                    in {
                        win["窗口编号"]
                        for win in committed["窗口"]
                        if any(member["观测编号"] == observation_no for member in win["窗口成员"])
                    }
                ],
            }
            if correction_entry is not None:
                result["更正审计"] = copy.deepcopy(self._corrections[-1])
            return result

    # ---------------------------------------------------------------- 计算引擎
    def _config_for(self, indicator: str) -> dict[str, Any] | None:
        return next((cfg for cfg in self._configs if cfg["指标"] == indicator), None)

    def _align_window(self, event: datetime, config: dict[str, Any]) -> datetime:
        origin = parse_time(config["窗口起点"])
        length = timedelta(minutes=int(config["窗口长度分钟"]))
        index = math.floor((event - origin).total_seconds() / length.total_seconds())
        return origin + index * length

    def _compute_all(
        self,
        observations: list[dict[str, Any]],
        rules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        active = sorted(
            [copy.deepcopy(row) for row in observations if row.get("状态") == "生效"],
            key=lambda row: (
                parse_time(row["事件时间"]),
                parse_time(row["接收时间"]),
                int(row["版本"]),
            ),
        )
        groups: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
        issues: list[dict[str, Any]] = []
        for row in active:
            config = self._config_for(row["指标"])
            if config is None:
                issues.append({
                    "观测编号": row["观测编号"],
                    "原因": "指标未配置窗口长度，未参与计算",
                })
                continue
            start = self._align_window(parse_time(row["事件时间"]), config)
            groups.setdefault((row["指标"], row["维度"], start), []).append(row)

        windows = [
            self._build_window(indicator, dimension, start, members, rules)
            for (indicator, dimension, start), members in groups.items()
        ]
        windows.sort(key=lambda win: (win["指标"], win["维度"], win["窗口开始"]))
        alerts: list[dict[str, Any]] = []
        for window in windows:
            for detail in window["触发明细"]:
                alerts.append(self._build_alert(window, detail))
        alerts.sort(key=lambda item: (item["指标"], item["维度"], item["窗口开始"], item["规则编号"]))
        return {"窗口": windows, "预警": alerts, "计算问题": issues}

    def _build_window(
        self,
        indicator: str,
        dimension: str,
        start: datetime,
        members: list[dict[str, Any]],
        rules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        config = self._config_for(indicator)
        if config is None:
            raise ValueError(f"指标「{indicator}」缺少窗口配置")
        length = timedelta(minutes=int(config["窗口长度分钟"]))
        interval = timedelta(minutes=int(config["采集间隔分钟"]))
        end = start + length
        window_id = window_id_for(indicator, dimension, start)
        # 示例/接口里的同一窗口只能有一条生效记录；事件时间完全相同的两条观测取后接收版本。
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in members:
            key = (row["观测编号"], row["事件时间"])
            if key not in deduped or int(row["版本"]) > int(deduped[key]["版本"]):
                deduped[key] = row
        members = list(deduped.values())

        ordered = sorted(
            members,
            key=lambda row: (
                parse_time(row["事件时间"]),
                parse_time(row["接收时间"]),
                int(row["版本"]),
            ),
        )
        gaps = self._detect_gaps(ordered, interval, end)

        indicator_rules = [rule for rule in rules if rule["指标"] == indicator]
        rule_numbers: list[str] = []
        for rule in indicator_rules:
            if rule["规则编号"] not in rule_numbers:
                rule_numbers.append(rule["规则编号"])
        rule_numbers.sort()

        member_rows: list[dict[str, Any]] = []
        judgements_by_obs: dict[str, dict[str, dict[str, Any]]] = {}
        uncovered: list[dict[str, Any]] = []
        for obs in ordered:
            event = parse_time(obs["事件时间"])
            judgement_list: list[dict[str, Any]] = []
            obs_judgements: dict[str, dict[str, Any]] = {}
            for rule_no in rule_numbers:
                versions = sorted(
                    [rule for rule in indicator_rules if rule["规则编号"] == rule_no],
                    key=lambda rule: parse_time(rule["生效区间"]["开始"]),
                )
                judgement = self._resolve_judgement(obs, event, rule_no, versions)
                judgement_list.append(judgement)
                obs_judgements[rule_no] = judgement
                if judgement.get("未覆盖生效区间"):
                    uncovered.append({
                        "窗口编号": window_id,
                        "观测编号": obs["观测编号"],
                        "规则编号": rule_no,
                        "事件时间": obs["事件时间"],
                        "说明": judgement["原因"],
                    })
            judgements_by_obs[obs["观测编号"]] = obs_judgements
            member_rows.append({
                "观测编号": obs["观测编号"],
                "指标": obs["指标"],
                "维度": obs["维度"],
                "事件时间": obs["事件时间"],
                "接收时间": obs["接收时间"],
                "数值": obs["数值"],
                "观测版本": obs["版本"],
                "备注": obs.get("备注", ""),
                "适用规则": judgement_list,
            })

        threshold_segments, _ = self._threshold_segments(
            window_id, indicator_rules, rule_numbers, ordered, judgements_by_obs
        )

        rule_judgements: list[dict[str, Any]] = []
        triggered_details: list[dict[str, Any]] = []
        for rule_no in rule_numbers:
            versions = sorted(
                [rule for rule in indicator_rules if rule["规则编号"] == rule_no],
                key=lambda rule: parse_time(rule["生效区间"]["开始"]),
            )
            judgement = self._judge_rule(
                indicator, dimension, window_id, rule_no, versions, ordered,
                judgements_by_obs, interval, start, end, gaps,
            )
            rule_judgements.append(judgement)
            if judgement["判定"] == "触发":
                triggered_details.append(judgement)

        if triggered_details:
            strongest = max(
                triggered_details,
                key=lambda item: (item["级别序"], item["最大超出比例"], item["规则编号"]),
            )
            level = strongest["预警级别"]
            level_value = strongest["级别序"]
            status = "有预警"
        else:
            level = "无"
            level_value = -1
            status = "无预警"

        return {
            "窗口编号": window_id,
            "窗口版本": 0,
            "指标": indicator,
            "维度": dimension,
            "窗口开始": iso_time(start),
            "窗口结束": iso_time(end),
            "窗口长度分钟": int(config["窗口长度分钟"]),
            "采集间隔分钟": int(config["采集间隔分钟"]),
            "观测数": len(ordered),
            "窗口成员": member_rows,
            "待补齐缺口": gaps,
            "实际阈值": threshold_segments,
            # 触发的规则排在前面，便于直接看到“哪些条件成立”；其余判定仍完整保留。
            "连续段": [
                segment
                for judgement in sorted(
                    rule_judgements, key=lambda item: item["判定"] != "触发"
                )
                for segment in judgement.get("连续段", [])
            ],
            "规则判定": rule_judgements,
            "触发明细": triggered_details,
            "预警级别": level,
            "级别序": level_value,
            "状态": status,
            "阈值未覆盖观测": uncovered,
            "阈值未覆盖": bool(uncovered),
        }

    def _detect_gaps(
        self,
        ordered: list[dict[str, Any]],
        interval: timedelta,
        window_end: datetime,
    ) -> list[dict[str, Any]]:
        """按采集间隔识别窗口内缺报；窗口末尾的缺报也要记，不能默认窗口完整。"""
        gaps: list[dict[str, Any]] = []
        for index, current in enumerate(ordered):
            current_time = parse_time(current["事件时间"])
            if index == 0:
                continue
            previous = ordered[index - 1]
            previous_time = parse_time(previous["事件时间"])
            if current_time - previous_time <= interval:
                continue
            expected = previous_time + interval
            guard = 0
            while expected < current_time - timedelta(seconds=1) and guard < 1000:
                gaps.append({
                    "期望时间": iso_time(expected),
                    "前序观测编号": previous["观测编号"],
                    "后续观测编号": current["观测编号"],
                    "位置": "窗口内部",
                    "说明": "该采集时刻未收到观测，记为待补齐缺口；缺口不按恢复正常处理",
                })
                expected += interval
                guard += 1
        if ordered:
            last = ordered[-1]
            expected = parse_time(last["事件时间"]) + interval
            guard = 0
            while expected < window_end - timedelta(seconds=1) and guard < 1000:
                gaps.append({
                    "期望时间": iso_time(expected),
                    "前序观测编号": last["观测编号"],
                    "后续观测编号": None,
                    "位置": "窗口末尾",
                    "说明": "窗口末尾采集未到，记为待补齐缺口；缺口不按恢复正常处理",
                })
                expected += interval
                guard += 1
        return gaps

    def _resolve_judgement(
        self,
        obs: dict[str, Any],
        event: datetime,
        rule_no: str,
        versions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        applicable_versions = [
            rule
            for rule in versions
            if parse_time(rule["生效区间"]["开始"]) <= event
            and (
                rule["生效区间"]["结束"] is None
                or event < parse_time(rule["生效区间"]["结束"])
            )
        ]
        if not applicable_versions:
            return {
                "规则编号": rule_no,
                "适用": False,
                "未覆盖生效区间": True,
                "原因": "事件时间不在该规则任一生效区间内，未用新阈值解释旧观测",
            }
        version = max(applicable_versions, key=lambda rule: parse_time(rule["生效区间"]["开始"]))
        if not scope_matches(version["适用范围"], obs["维度"]):
            return {
                "规则编号": rule_no,
                "规则版本": version["规则版本"],
                "适用": False,
                "未覆盖生效区间": False,
                "原因": f"适用范围「{version['适用范围']}」不包含维度「{obs['维度']}」",
            }
        threshold = float(version["阈值上限"])
        direction = version["方向"]
        exceeds = is_exceed(direction, float(obs["数值"]), threshold)
        ratio = exceed_ratio(direction, float(obs["数值"]), threshold) if exceeds else 0.0
        return {
            "规则编号": rule_no,
            "规则版本": version["规则版本"],
            "规则名称": version["规则名称"],
            "阈值上限": threshold,
            "方向": direction,
            "生效区间": copy.deepcopy(version["生效区间"]),
            "适用": True,
            "是否超限": exceeds,
            "超出比例": round(ratio, 6),
        }

    def _threshold_segments(
        self,
        window_id: str,
        indicator_rules: list[dict[str, Any]],
        rule_numbers: list[str],
        ordered: list[dict[str, Any]],
        judgements_by_obs: dict[str, dict[str, dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        segments: list[dict[str, Any]] = []
        uncovered: list[dict[str, Any]] = []
        for rule_no in rule_numbers:
            buckets: dict[int, dict[str, Any]] = {}
            for obs in ordered:
                judgement = judgements_by_obs[obs["观测编号"]][rule_no]
                if judgement.get("未覆盖生效区间"):
                    uncovered.append({
                        "窗口编号": window_id,
                        "观测编号": obs["观测编号"],
                        "规则编号": rule_no,
                        "事件时间": obs["事件时间"],
                        "说明": judgement["原因"],
                    })
                if not judgement.get("适用"):
                    continue
                version = int(judgement["规则版本"])
                bucket = buckets.setdefault(version, {
                    "规则编号": rule_no,
                    "规则版本": version,
                    "阈值上限": judgement["阈值上限"],
                    "方向": judgement["方向"],
                    "生效区间": copy.deepcopy(judgement["生效区间"]),
                    "观测编号": [],
                    "首次事件时间": obs["事件时间"],
                    "最后事件时间": obs["事件时间"],
                })
                bucket["观测编号"].append(obs["观测编号"])
                bucket["最后事件时间"] = obs["事件时间"]
            segments.extend(
                buckets[key] for key in sorted(buckets)
            )
        segments.sort(key=lambda item: (item["规则编号"], item["规则版本"]))
        return segments, uncovered

    def _judge_rule(
        self,
        indicator: str,
        dimension: str,
        window_id: str,
        rule_no: str,
        versions: list[dict[str, Any]],
        ordered: list[dict[str, Any]],
        judgements_by_obs: dict[str, dict[str, dict[str, Any]]],
        interval: timedelta,
        window_start: datetime,
        window_end: datetime,
        gaps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """计算一条规则在窗口内的判定。

        连续超限按事件时间顺序计算；缺报时刻既不算超限也不算恢复，只标为待补齐缺口。
        规则版本在观测事件时刻解析，窗口跨越阈值调整时同一条连续段可以带多个版本。
        """
        # 缺报以实际采集间隔推断（不假设采集相位），与窗口的待补齐缺口同源。
        missing_times = [gap["期望时间"] for gap in gaps]

        segments: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        all_exceeds: list[dict[str, Any]] = []
        evaluated: list[dict[str, Any]] = []
        applicable_count = 0

        def close_current() -> None:
            nonlocal current
            if current is None:
                return
            segments.append(current)
            current = None

        for obs in ordered:
            judgement = judgements_by_obs[obs["观测编号"]][rule_no]
            if not judgement.get("适用"):
                evaluated.append({
                    "观测编号": obs["观测编号"],
                    "事件时间": obs["事件时间"],
                    "数值": obs["数值"],
                    "是否超限": None,
                    "判定": "不适用",
                    "规则版本": judgement.get("规则版本"),
                    "原因": judgement["原因"],
                })
                continue
            applicable_count += 1
            if judgement.get("是否超限"):
                all_exceeds.append({
                    "观测编号": obs["观测编号"],
                    "事件时间": obs["事件时间"],
                    "数值": obs["数值"],
                    "规则版本": judgement["规则版本"],
                    "阈值上限": judgement["阈值上限"],
                    "超出比例": judgement["超出比例"],
                })
                if current is None:
                    current = {
                        "规则编号": rule_no,
                        "观测编号列表": [],
                        "起始事件时间": obs["事件时间"],
                        "结束事件时间": obs["事件时间"],
                        "规则版本序列": [],
                        "含缺口": False,
                        "缺口期望时间": [],
                        "跨阈值调整": False,
                    }
                current["观测编号列表"].append({
                    "观测编号": obs["观测编号"],
                    "事件时间": obs["事件时间"],
                    "数值": obs["数值"],
                    "规则版本": judgement["规则版本"],
                    "阈值上限": judgement["阈值上限"],
                })
                current["结束事件时间"] = obs["事件时间"]
                if judgement["规则版本"] not in current["规则版本序列"]:
                    current["规则版本序列"].append(judgement["规则版本"])
            else:
                close_current()
            evaluated.append({
                "观测编号": obs["观测编号"],
                "事件时间": obs["事件时间"],
                "数值": obs["数值"],
                "是否超限": bool(judgement.get("是否超限")),
                "判定": "超限" if judgement.get("是否超限") else "未超限",
                "不超限原因": (
                    "数值等于阈值，临界值不算超限"
                    if float(obs["数值"]) == float(judgement["阈值上限"])
                    else None
                ),
                "规则版本": judgement["规则版本"],
                "阈值上限": judgement["阈值上限"],
            })
        close_current()

        # 缺口只挂在相邻连续段上；窗口内存在缺口时，缺报前后的超限仍看作同一段。
        for segment in segments:
            contained = [
                moment for moment in missing_times
                if segment["起始事件时间"] < moment < segment["结束事件时间"]
            ]
            if contained:
                segment["含缺口"] = True
                segment["缺口期望时间"] = contained
            segment["跨阈值调整"] = len(segment["规则版本序列"]) > 1

        # 缺口若横跨两个连续段之间（中间有未超限观测），不合并，只标注在段尾。
        required = int(versions[-1]["连续超限次数"]) if versions else 1
        for segment in segments:
            segment["超限次数"] = len(segment["观测编号列表"])
            segment["达到连续要求"] = segment["超限次数"] >= required
            segment["状态"] = "已触发" if segment["达到连续要求"] else "未达触发次数"
            segment["实际阈值"] = [
                {
                    "规则版本": number,
                    "阈值上限": next(
                        (item["阈值上限"] for item in segment["观测编号列表"] if item["规则版本"] == number),
                        None,
                    ),
                }
                for number in segment["规则版本序列"]
            ]
            note = (
                f"连续超限 {segment['超限次数']} 次，"
                f"{'达到' if segment['达到连续要求'] else '未达到'}连续 {required} 次要求"
            )
            if segment["跨阈值调整"]:
                thresholds = "、".join(
                    f"v{item['规则版本']}={item['阈值上限']:g}" for item in segment["实际阈值"]
                )
                note += f"；段内跨阈值调整，逐条实际阈值 {thresholds}"
            if segment["含缺口"]:
                note += "；段内含待补齐缺口，缺口不视为恢复"
            segment["说明"] = note
        max_count = max((segment["超限次数"] for segment in segments), default=0)
        triggered_segments = [segment for segment in segments if segment["达到连续要求"]]

        base = {
            "规则编号": rule_no,
            "规则名称": versions[-1]["规则名称"] if versions else rule_no,
            "规则版本": sorted({rule["规则版本"] for rule in versions}),
            "方向": versions[-1]["方向"] if versions else "gt",
            "阈值上限": sorted({float(rule["阈值上限"]) for rule in versions}),
            "连续超限次数": required,
            "实际连续次数": max_count,
            "超限观测编号": [item["观测编号"] for item in all_exceeds],
            "连续段": segments,
            "判定依据": {
                "窗口编号": window_id,
                "指标": indicator,
                "维度": dimension,
                "窗口开始": iso_time(window_start),
                "窗口结束": iso_time(window_end),
                "逐条判定": evaluated,
                "超限明细": all_exceeds,
                "缺报时刻": missing_times,
            },
        }
        if applicable_count == 0:
            base.update({
                "判定": "未触发",
                "原因": "窗口内没有适用该规则的观测（适用范围或阈值生效区间不覆盖）",
            })
            return base
        if not triggered_segments:
            if not all_exceeds:
                reason = "窗口内没有严格越过阈值的观测"
            else:
                reason = (
                    f"最大连续超限 {max_count} 次，未达到连续 {required} 次要求；"
                    "断续超限证据保留，缺报不按恢复处理"
                )
            base.update({"判定": "未触发", "原因": reason})
            return base

        level, rank, max_ratio, grade_basis = self._grade_triggered(versions, all_exceeds)
        base.update({
            "判定": "触发",
            "预警级别": level,
            "级别序": rank,
            "最大超出比例": round(max_ratio, 6),
            "分级依据": grade_basis,
            "实际连续次数": max(
                segment["超限次数"] for segment in triggered_segments
            ),
            "触发连续段": [
                {
                    "观测编号列表": [item["观测编号"] for item in segment["观测编号列表"]],
                    "超限次数": segment["超限次数"],
                    "含缺口": segment["含缺口"],
                    "缺口期望时间": segment["缺口期望时间"],
                    "规则版本序列": segment["规则版本序列"],
                }
                for segment in triggered_segments
            ],
        })
        return base

    def _grade_triggered(
        self, versions: list[dict[str, Any]], exceeds: list[dict[str, Any]]
    ) -> tuple[str, int, float, str]:
        by_version: dict[int, list[dict[str, Any]]] = {}
        for item in exceeds:
            by_version.setdefault(int(item["规则版本"]), []).append(item)
        candidates: list[tuple[int, str, float, str]] = []
        version_map = {int(rule["规则版本"]): rule for rule in versions}
        for version_number, items in by_version.items():
            rule = version_map.get(version_number)
            if rule is None:
                continue
            max_ratio = max(float(item["超出比例"]) for item in items)
            chosen = rule["分级标准"][0]
            for index, level in enumerate(rule["分级标准"]):
                if max_ratio + 1e-12 >= float(level["最小超出比例"]):
                    chosen = level
            rank = level_rank(chosen["级别"], rule["分级标准"], index)
            basis = (
                f"规则版本 {version_number} 阈值 {rule['阈值上限']:g}，"
                f"最大超出比例 {max_ratio:.2%}，对应分级「{chosen['级别']}」"
            )
            candidates.append((
                rank,
                chosen["级别"],
                max_ratio,
                basis,
            ))
        if not candidates:
            return "一般", level_rank("一般", []), 0.0, "无可用分级标准"
        candidates.sort(key=lambda item: (item[0], item[2]), reverse=True)
        rank, level, ratio, basis = candidates[0]
        return level, rank, ratio, basis

    def _build_alert(self, window: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
        return {
            "预警编号": alert_id_for(window["窗口编号"], detail["规则编号"]),
            "窗口编号": window["窗口编号"],
            "窗口版本": window["窗口版本"],
            "指标": window["指标"],
            "维度": window["维度"],
            "窗口开始": window["窗口开始"],
            "窗口结束": window["窗口结束"],
            "规则编号": detail["规则编号"],
            "规则名称": detail["规则名称"],
            "规则版本": copy.deepcopy(detail["规则版本"]),
            "预警级别": detail["预警级别"],
            "级别序": detail["级别序"],
            "最大超出比例": detail.get("最大超出比例", 0.0),
            "实际阈值": copy.deepcopy(
                [
                    segment
                    for segment in window["实际阈值"]
                    if segment["规则编号"] == detail["规则编号"]
                ]
            ),
            "阈值上限": copy.deepcopy(detail["阈值上限"]),
            "连续超限次数": detail["连续超限次数"],
            "实际连续次数": detail["实际连续次数"],
            "触发依据": {
                "分级依据": detail["分级依据"],
                "超限观测编号": copy.deepcopy(detail["超限观测编号"]),
                "触发连续段": copy.deepcopy(detail["触发连续段"]),
                "含缺口": any(segment["含缺口"] for segment in detail["触发连续段"]),
                "规则版本": copy.deepcopy(detail["规则版本"]),
            },
            "窗口成员数": len(window["窗口成员"]),
            "窗口待补齐缺口": [gap["期望时间"] for gap in window["待补齐缺口"]],
            "状态": "待处置",
            "预警版本": 1,
            "修订记录": [],
            "数据集版本": self._dataset.get("数据集版本", 0) + 1,
        }

    # ---------------------------------------------------------------- 提交/版本
    def _prepare_correction_entry(
        self,
        previous: dict[str, Any],
        new_row: dict[str, Any],
        computed: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """生成更正审计：记录原预警是否被更正、依据是什么，不删除既有结论。"""
        obs_no = new_row["观测编号"]
        prior = self._dataset
        affected = {
            window["窗口编号"]
            for window in prior.get("窗口", [])
            if any(member["观测编号"] == obs_no for member in window["窗口成员"])
        } | {
            window["窗口编号"]
            for window in computed["窗口"]
            if any(member["观测编号"] == obs_no for member in window["窗口成员"])
        }
        prior_alerts = [
            alert for alert in prior.get("预警", []) if alert["窗口编号"] in affected
        ]
        # 新预警此刻还没有版本/状态，用窗口内触发明细描述结论。
        new_alerts = [
            alert for alert in computed["预警"] if alert["窗口编号"] in affected
        ]
        prior_map = {alert["预警编号"]: alert for alert in prior_alerts}
        new_map = {alert["预警编号"]: alert for alert in new_alerts}
        changed: list[str] = []
        for alert_id, old in prior_map.items():
            new = new_map.get(alert_id)
            if new is None:
                changed.append(f"原预警 {alert_id}（{old['预警级别']}）不再触发，标记为已更正并保留原处置依据")
            elif old["预警级别"] != new["预警级别"]:
                changed.append(
                    f"原预警 {alert_id} 由「{old['预警级别']}」调整为「{new['预警级别']}」"
                )
        added = [alert_id for alert_id in new_map if alert_id not in prior_map]
        for alert_id in added:
            changed.append(f"新增预警 {alert_id}（{new_map[alert_id]['预警级别']}）")
        basis = new_row.get("更正依据") or str(values.get("备注") or "").strip()
        auto_basis = (
            f"观测 {obs_no} 的事件时间 {previous['事件时间']} 数值由 {previous['数值']:g} "
            f"更正为 {new_row['数值']:g}；按事件时间重算窗口成员、实际阈值与连续超限段"
        )
        return {
            "id": self._next_correction_id,
            "观测编号": obs_no,
            "原版本": previous["版本"],
            "新版本": new_row["版本"],
            "记录时间": iso_time(datetime.now()),
            "原内容": {
                "指标": previous["指标"],
                "维度": previous["维度"],
                "事件时间": previous["事件时间"],
                "接收时间": previous["接收时间"],
                "数值": previous["数值"],
            },
            "新内容": {
                "指标": new_row["指标"],
                "维度": new_row["维度"],
                "事件时间": new_row["事件时间"],
                "接收时间": new_row["接收时间"],
                "数值": new_row["数值"],
            },
            "更正依据": basis or auto_basis,
            "自动依据": auto_basis,
            "影响窗口": sorted(affected),
            "原预警结论": [
                {
                    "预警编号": alert["预警编号"],
                    "窗口编号": alert["窗口编号"],
                    "窗口版本": alert["窗口版本"],
                    "预警级别": alert["预警级别"],
                    "状态": alert["状态"],
                }
                for alert in prior_alerts
            ],
            "新预警结论": [
                {
                    "预警编号": alert["预警编号"],
                    "窗口编号": alert["窗口编号"],
                    "规则编号": alert["规则编号"],
                    "预警级别": alert["预警级别"],
                }
                for alert in new_alerts
            ],
            "是否更正原预警": any(
                new_map.get(alert["预警编号"]) is None
                or new_map[alert["预警编号"]]["预警级别"] != alert["预警级别"]
                for alert in prior_alerts
            ),
            "变更摘要": changed or ["窗口成员已更新，但既有预警级别与触发结论未变化"],
        }

    def _commit_dataset(
        self,
        computed: dict[str, Any],
        *,
        reason: str = "重算",
        correction: dict[str, Any] | None = None,
        prior_dataset: dict[str, Any] | None = None,
        prior_corrections: list[dict[str, Any]] | None = None,
    ) -> None:
        """把计算结果整体提交。

        先在副本上构建全部窗口/预警/更正审计，构建过程中不改动任何既有状态；
        构建完成后才一次性替换，调用方抛错时旧数据集与旧审计都完整保留，
        不会出现“一半旧分级、一半新分级”。
        """
        prior = copy.deepcopy(prior_dataset if prior_dataset is not None else self._dataset)
        prior_windows = {window["窗口编号"]: window for window in prior.get("窗口", [])}
        prior_alerts = {alert["预警编号"]: alert for alert in prior.get("预警", [])}
        correction_obs = correction["观测编号"] if correction else None

        windows: list[dict[str, Any]] = []
        for window in computed["窗口"]:
            old = prior_windows.get(window["窗口编号"])
            stamp = {key: value for key, value in window.items() if key != "窗口版本"}
            if old is None:
                window["窗口版本"] = 1
            else:
                old_stamp = {key: value for key, value in old.items() if key != "窗口版本"}
                if content_signature(stamp) == content_signature(old_stamp):
                    window["窗口版本"] = old["窗口版本"]
                else:
                    window["窗口版本"] = int(old["窗口版本"]) + 1
            windows.append(window)

        alerts: list[dict[str, Any]] = []
        for alert in computed["预警"]:
            window = next(item for item in windows if item["窗口编号"] == alert["窗口编号"])
            alert["窗口版本"] = window["窗口版本"]
            old = prior_alerts.get(alert["预警编号"])
            if old is None:
                alerts.append(alert)
                continue
            status = old.get("状态", "待处置")
            alert["状态"] = "待处置" if status == "已更正" else status
            alert["预警版本"] = int(old.get("预警版本", 1))
            alert["修订记录"] = copy.deepcopy(old.get("修订记录", []))
            changes = self._alert_changes(old, alert)
            if changes:
                alert["预警版本"] = int(alert["预警版本"]) + 1
                alert["修订记录"].append({
                    "修订时间": iso_time(datetime.now()),
                    "类型": "迟到更正" if correction_obs else reason,
                    "依据观测": correction_obs,
                    "原级别": old["预警级别"],
                    "新级别": alert["预警级别"],
                    "变化": changes,
                    "说明": (
                        correction["更正依据"] if correction else "重算后预警依据发生变化"
                    ),
                })
            alerts.append(alert)

        active_ids = {alert["预警编号"] for alert in alerts}
        retired = copy.deepcopy(prior.get("已更正预警", []))
        for old_id, old in prior_alerts.items():
            if old_id in active_ids:
                continue
            retired.append({
                "预警编号": old_id,
                "窗口编号": old["窗口编号"],
                "窗口版本": old["窗口版本"],
                "指标": old["指标"],
                "维度": old["维度"],
                "窗口开始": old["窗口开始"],
                "窗口结束": old["窗口结束"],
                "规则编号": old["规则编号"],
                "规则名称": old["规则名称"],
                "原预警级别": old["预警级别"],
                "原状态": old["状态"],
                "原实际阈值": copy.deepcopy(old.get("实际阈值", [])),
                "原触发依据": copy.deepcopy(old.get("触发依据", {})),
                "原修订记录": copy.deepcopy(old.get("修订记录", [])),
                "状态": "已更正",
                "更正时间": iso_time(datetime.now()),
                "更正类型": "迟到更正" if correction_obs else reason,
                "更正依据": (
                    correction["更正依据"]
                    if correction
                    else "重算后窗口不再满足连续超限条件；保留原预警作为历史处置依据"
                ),
                "更正说明": (
                    "原预警不再触发，已标记为已更正并保留全部原始依据，未删除历史结论"
                ),
            })

        if correction is not None:
            correction["原预警结论"] = [
                {
                    "预警编号": alert["预警编号"],
                    "窗口编号": alert["窗口编号"],
                    "窗口版本": alert["窗口版本"],
                    "预警级别": alert["预警级别"],
                    "状态": alert["状态"],
                }
                for alert in prior_alerts.values()
                if alert["窗口编号"] in correction["影响窗口"]
            ]
            correction["新预警结论"] = [
                {
                    "预警编号": alert["预警编号"],
                    "窗口编号": alert["窗口编号"],
                    "窗口版本": alert["窗口版本"],
                    "规则编号": alert["规则编号"],
                    "预警级别": alert["预警级别"],
                }
                for alert in alerts
                if alert["窗口编号"] in correction["影响窗口"]
            ]
            correction["是否更正原预警"] = any(
                item["预警编号"] not in active_ids
                or next(
                    (alert["预警级别"] for alert in alerts if alert["预警编号"] == item["预警编号"]),
                    item["预警级别"],
                ) != item["预警级别"]
                for item in correction["原预警结论"]
            )

        level_stats: dict[str, int] = {}
        for alert in alerts:
            level_stats[alert["预警级别"]] = level_stats.get(alert["预警级别"], 0) + 1
        flat_gaps = [
            {
                "窗口编号": window["窗口编号"],
                "期望时间": gap["期望时间"],
                "前序观测编号": gap["前序观测编号"],
                "后续观测编号": gap["后续观测编号"],
            }
            for window in windows
            for gap in window["待补齐缺口"]
        ]
        uncovered = [
            item for window in windows for item in window["阈值未覆盖观测"]
        ]
        active_observations = [row for row in self._observations if row["状态"] == "生效"]
        dataset: dict[str, Any] = {
            "数据集版本": int(prior.get("数据集版本", 0)),
            "生成时间": iso_time(datetime.now()),
            "观测数": len(active_observations),
            "重复观测忽略数": sum(int(row.get("重复次数", 0)) for row in self._observations),
            "窗口数": len(windows),
            "预警数": len(alerts),
            "级别统计": level_stats,
            "窗口": windows,
            "预警": alerts,
            "已更正预警": retired,
            "待补齐缺口": flat_gaps,
            "阈值未覆盖观测": uncovered,
        }
        signature_payload = {
            "窗口": [
                {key: value for key, value in window.items() if key != "窗口版本"}
                for window in windows
            ],
            "预警": [
                {
                    key: value
                    for key, value in alert.items()
                    if key not in {"数据集版本", "修订记录"}
                }
                for alert in alerts
            ],
            "已更正预警": retired,
            "待补齐缺口": flat_gaps,
        }
        prior_signature_payload = {
            "窗口": [
                {key: value for key, value in window.items() if key != "窗口版本"}
                for window in prior.get("窗口", [])
            ],
            "预警": [
                {
                    key: value
                    for key, value in alert.items()
                    if key not in {"数据集版本", "修订记录"}
                }
                for alert in prior.get("预警", [])
            ],
            "已更正预警": prior.get("已更正预警", []),
            "待补齐缺口": prior.get("待补齐缺口", []),
        }
        if int(prior.get("数据集版本", 0)) == 0 or content_signature(signature_payload) != content_signature(prior_signature_payload):
            dataset["数据集版本"] = int(prior.get("数据集版本", 0)) + 1
        for alert in dataset["预警"]:
            alert["数据集版本"] = dataset["数据集版本"]

        # 修正更正审计中新预警的版本号（此前计算时窗口尚未赋版本）。
        if correction is not None:
            correction["新预警结论"] = [
                {
                    "预警编号": alert["预警编号"],
                    "窗口编号": alert["窗口编号"],
                    "窗口版本": alert["窗口版本"],
                    "规则编号": alert["规则编号"],
                    "预警级别": alert["预警级别"],
                }
                for alert in alerts
                if alert["窗口编号"] in correction["影响窗口"]
            ]
        # 所有结构都构建完成后才替换；数据集与审计一起提交，异常时两者都保持旧值。
        pending_corrections = copy.deepcopy(
            prior_corrections if prior_corrections is not None else self._corrections
        )
        if correction is not None:
            pending_corrections.append(correction)
        self._dataset = dataset
        self._corrections = pending_corrections
        if correction is not None:
            self._next_correction_id = max(self._next_correction_id, correction["id"] + 1)
        self._retired_alerts = retired

    def _alert_changes(self, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
        changes: list[str] = []
        if old["预警级别"] != new["预警级别"]:
            changes.append(f"预警级别：{old['预警级别']} -> {new['预警级别']}")
        if old.get("实际连续次数") != new.get("实际连续次数"):
            changes.append(
                f"实际连续超限：{old.get('实际连续次数')} -> {new.get('实际连续次数')}"
            )
        old_obs = set(old.get("触发依据", {}).get("超限观测编号", []))
        new_obs = set(new.get("触发依据", {}).get("超限观测编号", []))
        if old_obs != new_obs:
            changes.append(
                "超限观测变化：新增 " + "、".join(sorted(new_obs - old_obs) or ["无"])
                + "；移除 " + "、".join(sorted(old_obs - new_obs) or ["无"])
            )
        if content_signature(old.get("实际阈值")) != content_signature(new.get("实际阈值")):
            changes.append("实际适用阈值或观测归属发生变化")
        return changes

    # ---------------------------------------------------------------- 对外读取
    def recompute(self, reason: str = "手动重算") -> dict[str, Any]:
        with self._lock:
            prior_version = self._dataset["数据集版本"]
            try:
                computed = self._compute_all(self._observations, self._rules)
            except ValueError as error:
                raise ValueError(f"窗口计算失败，旧数据集保持不变：{error}")
            self._commit_dataset(computed, reason=reason)
            return {
                "ok": True,
                "数据集版本": self._dataset["数据集版本"],
                "原数据集版本": prior_version,
                "窗口数": self._dataset["窗口数"],
                "预警数": self._dataset["预警数"],
                "说明": (
                    "重算完成，数据集内容无变化"
                    if self._dataset["数据集版本"] == prior_version
                    else "重算完成，分级数据集已整体更新"
                ),
            }

    def dataset(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._dataset)

    def list_windows(self, *, keyword: str | None = None, level: str | None = None) -> dict[str, Any]:
        with self._lock:
            dataset = copy.deepcopy(self._dataset)
        windows = dataset["窗口"]
        if keyword:
            windows = [
                window
                for window in windows
                if keyword in window["指标"]
                or keyword in window["维度"]
                or keyword in window["窗口编号"]
            ]
        if level:
            windows = [window for window in windows if window["预警级别"] == level]
        return {
            "数据集版本": dataset["数据集版本"],
            "生成时间": dataset["生成时间"],
            "total": len(windows),
            "items": windows,
            "级别统计": dataset["级别统计"],
            "待补齐缺口": dataset["待补齐缺口"],
        }

    def get_window(self, window_id: str) -> dict[str, Any] | None:
        with self._lock:
            dataset = copy.deepcopy(self._dataset)
        for window in dataset["窗口"]:
            if window["窗口编号"] == window_id:
                related = [
                    alert for alert in dataset["预警"] if alert["窗口编号"] == window_id
                ]
                return {**window, "关联预警": related}
        return None

    def corrections(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._corrections)

    def alerts(self, *, status: str | None = None, level: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = copy.deepcopy(self._dataset["预警"])
        if status:
            rows = [row for row in rows if row["状态"] == status]
        if level:
            rows = [row for row in rows if row["预警级别"] == level]
        return rows

    def update_alert_status(self, alert_id: str, action: str) -> tuple[dict[str, Any] | None, str]:
        """处置动作只改状态，不动依据；已更正预警不允许被“复活”。"""
        action_rules = {
            "确认预警": ("待处置", "已确认"),
            "执行处理": ("已确认", "已处理"),
            "关闭预警": ("已处理", "已关闭"),
        }
        if action not in action_rules:
            return None, f"动作「{action}」不属于窗口预警可执行范围"
        with self._lock:
            target = next(
                (row for row in self._dataset["预警"] if row["预警编号"] == alert_id), None
            )
            if target is None:
                return None, f"窗口预警 {alert_id} 不存在或已归档"
            source, destination = action_rules[action]
            if target["状态"] != source:
                return None, (
                    f"预警 {alert_id} 当前状态为「{target['状态']}」，"
                    f"不能执行「{action}」（要求「{source}」）"
                )
            target["状态"] = destination
            return copy.deepcopy(target), f"窗口预警已{action}"


class AlertService(WindowAlertService):
    """兼容原有“预警记录”接口：记录维护 + 窗口预警能力都在同一个服务实例上。"""

    MODULE = "alert"
    REQUIRED_FIELDS = ["记录编号", "预警类型", "预警级别"]
    STATUS_ORDER = ["未处理", "已确认", "已处理", "已关闭"]
    ACTION_RULES = {"确认预警": "未处理", "执行处理": "已确认", "关闭预警": "已处理"}
    NEGATIVE_ACTIONS: list[str] = []

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(self.MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.REQUIRED_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"预警记录 {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于异常预警中心可执行范围"
        target = self.ACTION_RULES[action]
        if target not in self.STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        return entry, f"预警记录已{action}"


alert_service = AlertService()
