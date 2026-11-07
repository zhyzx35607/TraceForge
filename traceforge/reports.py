"""Portable allowlisted snapshots; content hashes provide integrity, not attestations."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter

from .models import Patch
from .retrieval import redact
from .runner import assess_regression
from .store import patch_digest

LIMITS = {'events': 1000, 'evidence': 100, 'patches': 10, 'citations': 30, 'limitations': 20}
MODE_LABELS = {
    'replay': '离线固定样例回放：候选补丁来自已知样例，不代表模型诊断或修复能力。',
    'investigate': '仅检索调查：未调用模型形成根因结论，不代表已修复故障。',
    'live': '真实模型模式：只记录本次运行结果，单次运行不能得出模型成功率。',
}
REPORT_LIMITATIONS = [
    '诊断文字、标题与路径仍可能包含业务信息；字段白名单和脱敏不是匿名化保证。',
    '用量仅统计服务端已返回并记录的值；失败或中断请求的零值不代表零消耗、零费用。',
    '观察器与测试处于同一 Python 进程，反射或 monkey-patching 不具备密码学防篡改保证。',
    '本报告是本地记录快照；内容摘要仅用于校验文件内容完整性，不是签名或外部证明。',
]


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _integer(value, minimum=0, maximum=10**15):
    return value if type(value) is int and minimum <= value <= maximum else None


def _duration(value):
    return value if type(value) in {int, float} and 0 <= value <= 10**12 and math.isfinite(value) else None


class _Sanitizer:
    def __init__(self, project: dict, secrets: tuple[str, ...]):
        self.secrets = sorted({value for value in secrets if isinstance(value, str) and value}, key=len, reverse=True)
        root = project.get('root')
        self.roots = sorted({root, root.replace('\\', '/'), root.replace('/', '\\')}, key=len, reverse=True) \
            if isinstance(root, str) and root else []

    def text(self, value, limit=2000):
        if not isinstance(value, str):
            return None
        # Oversize unknown inputs are omitted, rather than processed without a bound
        # or sliced through a credential before replacement.
        if len(value) > 100_000:
            return '[字段超过导出上限]'
        for secret in self.secrets:
            value = value.replace(secret, '[REDACTED]')
        for root in self.roots:
            value = re.sub(re.escape(root), '[PROJECT_ROOT]', value, flags=re.IGNORECASE)
        value = redact(value)
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]', '', value)
        value = value.encode('utf-8', errors='replace').decode('utf-8')
        return value if len(value) <= limit else value[:limit] + '…[已截断]'

    def strings(self, value, maximum=LIMITS['citations'], limit=200):
        return [self.text(item, limit) for item in _list(value)[:maximum] if isinstance(item, str)]

    def digest(self, value):
        return self.text(value, 64) if isinstance(value, str) and re.fullmatch(r'[a-fA-F0-9]{64}', value) else None


def _source_hash(value):
    if isinstance(value, str) and len(value) <= 30_000:
        try:
            return hashlib.sha256(value.encode('utf-8')).hexdigest()
        except UnicodeError:
            pass
    return None


def _observer_valid(tests: dict) -> bool:
    if (tests.get('source') != 'external_observer' or tests.get('completed') is not True
            or tests.get('framework') not in ('unittest', 'pytest')):
        return False
    counts = {name: _integer(tests.get(name), maximum=2500) for name in ('executed', 'passed', 'failed', 'skipped')}
    if any(value is None for value in counts.values()) or counts['executed'] != sum(
            counts[name] for name in ('passed', 'failed', 'skipped')):
        return False
    identities, outcomes = tests.get('identities'), tests.get('outcomes')
    if (not isinstance(identities, list) or len(identities) != counts['executed']
            or not isinstance(outcomes, dict) or len(outcomes) != len(identities)):
        return False
    if any(not isinstance(value, str) or not value or len(value) > 500 for value in identities):
        return False
    if len(set(identities)) != len(identities) or set(outcomes) != set(identities):
        return False
    if any(value not in ('passed', 'failed', 'skipped') for value in outcomes.values()):
        return False
    actual = Counter(outcomes.values())
    return all(actual[name] == counts[name] for name in ('passed', 'failed', 'skipped'))


def _phase(raw, clean: _Sanitizer) -> dict:
    phase = _dict(raw)
    tests = _dict(phase.get('tests'))
    return {
        'exit_code': _integer(phase.get('exit_code'), minimum=-255, maximum=65535),
        'duration_ms': _duration(phase.get('duration_ms')),
        'timed_out': phase.get('timed_out') is True,
        'observation_status': clean.text(phase.get('observation_status'), 80),
        'tests': {'source': clean.text(tests.get('source'), 80),
                  'framework': clean.text(tests.get('framework'), 80),
                  'completed': tests.get('completed') is True,
                  'valid_observer': _observer_valid(tests),
                  **{name: _integer(tests.get(name), maximum=2500)
                     for name in ('executed', 'passed', 'failed', 'skipped')}},
    }


def _verified(run: dict, state: dict, verification: dict) -> tuple[bool, list[str]]:
    reasons = []
    if run.get('mode') not in ('live', 'replay'):
        reasons.append('当前模式不提供补丁验证结论。')
    if run.get('status') != 'completed':
        reasons.append('任务尚未以 completed 状态结束，不认定修复已验证。')
    if verification.get('regression_fixed') is not True:
        reasons.append('运行记录没有确认本次补丁修复了回归。')
    baseline, patched = _dict(verification.get('baseline')), _dict(verification.get('patched'))
    valid = all(_observer_valid(_dict(phase.get('tests'))) for phase in (baseline, patched))
    if not valid:
        reasons.append('缺少完整且一致的外部观察器记录，退出码或旧版日志计数不足以证实修复。')
    elif any(_integer(phase.get('exit_code'), minimum=-255, maximum=65535) is None
             or phase.get('timed_out') is True or phase.get('observation_status') != 'completed'
             for phase in (baseline, patched)):
        reasons.append('执行状态不完整或存在超时，不认定修复已验证。')
    else:
        _, assessment = assess_regression({'baseline': baseline, 'patched': patched})
        reasons.extend(assessment)
    approval = _dict(state.get('approval'))
    digest = state.get('patch_digest')
    patches = _list(_dict(state.get('diagnosis')).get('patches'))
    bound = False
    if 0 < len(patches) <= LIMITS['patches']:
        try:
            normalized = [Patch.model_validate(patch, strict=True).model_dump() for patch in patches]
            bound = patch_digest(normalized) == digest
        except (ValueError, TypeError, UnicodeError):
            pass
    if (not bound or approval.get('decision') != 'approve'
            or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
            or approval.get('patch_digest') != digest):
        reasons.append('缺少与当前补丁摘要绑定的批准记录。')
    return not reasons, reasons


def _select_evidence(diagnosis: dict, patches: list, evidence: list) -> tuple[list, list[str], list[str]]:
    requested = []
    for value in [diagnosis, *patches[:LIMITS['patches']]]:
        for identity in _list(_dict(value).get('evidence_ids'))[:LIMITS['citations']]:
            if isinstance(identity, str) and len(identity) <= 100_000 and identity not in requested:
                requested.append(identity)
    bounded = [(index, item) for index, item in enumerate(evidence[:10_000]) if isinstance(item, dict)]
    by_id = {item['id']: (index, item) for index, item in bounded
             if isinstance(item.get('id'), str) and len(item['id']) <= 100_000}
    selected = [by_id[identity] for identity in requested if identity in by_id][:LIMITS['evidence']]
    chosen = {index for index, _ in selected}
    for index, item in bounded:
        if len(selected) >= LIMITS['evidence']:
            break
        if index not in chosen:
            selected.append((index, item))
            chosen.add(index)
    exported = [item for _, item in selected]
    exported_ids = {item.get('id') for item in exported if isinstance(item.get('id'), str)}
    return exported, requested, [identity for identity in requested if identity not in exported_ids]


def build_report(run: dict, project: dict, events: list[dict], *,
                 secrets: tuple[str, ...] = (), events_truncated: bool = False) -> dict:
    """Build a bounded snapshot without changing inputs or reading external state."""
    run, project = _dict(run), _dict(project)
    clean = _Sanitizer(project, secrets)
    state = _dict(run.get('state'))
    diagnosis, verification = _dict(state.get('diagnosis')), _dict(state.get('verification'))
    evidence, patches, events = _list(state.get('evidence')), _list(diagnosis.get('patches')), _list(events)
    mode = run.get('mode') if isinstance(run.get('mode'), str) and run.get('mode') in MODE_LABELS else 'unknown'
    approval, usage = _dict(state.get('approval')), _dict(state.get('usage'))
    verified, reasons = _verified(run, state, verification)
    selected_evidence, requested_ids, unavailable_ids = _select_evidence(diagnosis, patches, evidence)
    report = {
        'schema_version': 1, 'kind': 'traceforge-run-report',
        'run': {'id': clean.text(run.get('id'), 200), 'title': clean.text(run.get('title'), 400),
                'query': clean.text(run.get('query'), 6000), 'mode': mode,
                'mode_label': MODE_LABELS.get(mode, '未知运行模式，不能推断是否调用模型或确认修复。'),
                'status': clean.text(run.get('status'), 80), 'created_at': clean.text(run.get('created_at'), 80),
                'updated_at': clean.text(run.get('updated_at'), 80)},
        'project': {'id': clean.text(project.get('id'), 200), 'name': clean.text(project.get('name'), 400)},
        'diagnosis': {'summary': clean.text(diagnosis.get('summary'), 4000),
                      'root_cause': clean.text(diagnosis.get('root_cause'), 8000),
                      'confidence': clean.text(diagnosis.get('confidence'), 30),
                      'limitations': clean.strings(diagnosis.get('limitations'), LIMITS['limitations'], 2000),
                      'evidence_ids': clean.strings(diagnosis.get('evidence_ids'))},
        'evidence': [{'id': clean.text(item.get('id'), 200), 'path': clean.text(item.get('path'), 500),
                      'line_start': _integer(item.get('line_start'), minimum=1),
                      'line_end': _integer(item.get('line_end'), minimum=1),
                      'source': clean.text(item.get('source'), 500), 'kind': clean.text(item.get('kind'), 80),
                      'sha256': clean.digest(item.get('sha256'))}
                     for item in selected_evidence],
        'citations': {'requested_ids': clean.strings(requested_ids, 330),
                      'unavailable_evidence_ids': clean.strings(unavailable_ids, 330),
                      'complete': not unavailable_ids, 'evidence_scan_limited': len(evidence) > 10_000},
        'patches': [{'path': clean.text(item.get('path'), 500), 'reason': clean.text(item.get('reason'), 2000),
                     'evidence_ids': clean.strings(item.get('evidence_ids')),
                     'before_sha256': clean.digest(_source_hash(item.get('before'))),
                     'after_sha256': clean.digest(_source_hash(item.get('after')))}
                    for item in patches[:LIMITS['patches']] if isinstance(item, dict)],
        'approval': {'decision': clean.text(approval.get('decision'), 80),
                     'patch_digest': clean.digest(approval.get('patch_digest')),
                     'approved_at': clean.text(approval.get('approved_at'), 80)},
        'verification': {'available': bool(verification), 'verified': verified, 'regression_fixed': verified,
                         'reported_regression_fixed': verification.get('regression_fixed') is True,
                         'runner': clean.text(verification.get('runner'), 80),
                         'baseline': _phase(verification.get('baseline'), clean),
                         'patched': _phase(verification.get('patched'), clean),
                         'limitations': reasons + clean.strings(verification.get('limitations'), LIMITS['limitations'], 2000)},
        'usage': {key: _integer(usage.get(key)) for key in ('provider_calls', 'input_tokens', 'output_tokens')},
        'events': [{'id': _integer(item.get('id')), 'kind': clean.text(item.get('kind'), 120),
                    'created_at': clean.text(item.get('created_at'), 80)}
                   for item in events[:LIMITS['events']] if isinstance(item, dict)],
        'counts': {'events_supplied': len(events), 'evidence_total': len(evidence), 'patches_total': len(patches)},
        'limits': dict(LIMITS),
        'events_truncated': bool(events_truncated) or len(events) > LIMITS['events'],
        'evidence_truncated': len(evidence) > LIMITS['evidence'], 'patches_truncated': len(patches) > LIMITS['patches'],
        'limitations': list(REPORT_LIMITATIONS),
        'integrity': {'algorithm': 'sha256',
                      'canonicalization': 'JSON UTF-8; ensure_ascii=False; sort_keys=True; separators=(",", ":"); omit content_sha256',
                      'meaning': '规范 JSON 内容完整性校验；不是 Markdown 文件哈希、签名或外部证明。'},
    }
    report['counts'].update(events_exported=len(report['events']), evidence_exported=len(report['evidence']),
                            patches_exported=len(report['patches']))
    report['content_sha256'] = hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return report


def _markdown(value) -> str:
    if value is None:
        return '未记录'
    if type(value) in {int, float}:
        return str(value) if abs(value) <= 10**15 and math.isfinite(value) else '未记录'
    if not isinstance(value, str):
        return '未记录'
    # Entity-encode punctuation before Markdown parsing. This prevents HTML,
    # links, images, reference definitions, code fences and GFM URL autolinks.
    return ''.join(f'&#{ord(char)};' if char in '\\`*_{}[]()<>#+-.!|&\"\'/:=@' else
                   ' ' if char.isspace() else char for char in value)


def render_markdown(report: dict) -> str:
    """Render only allowlisted fields as escaped text, never embedded media."""
    report = _dict(report)
    run, project = _dict(report.get('run')), _dict(report.get('project'))
    diagnosis, verification = _dict(report.get('diagnosis')), _dict(report.get('verification'))
    approval, usage = _dict(report.get('approval')), _dict(report.get('usage'))
    lines = ['# TraceForge 运行报告', '', f'项目：{_markdown(project.get("name"))}',
             f'任务：{_markdown(run.get("title"))}', f'运行 ID：{_markdown(run.get("id"))}',
             f'状态：{_markdown(run.get("status"))}', f'模式：{_markdown(run.get("mode_label"))}',
             f'创建时间：{_markdown(run.get("created_at"))}', f'更新时间：{_markdown(run.get("updated_at"))}',
             '', '## 问题与诊断', '', f'问题：{_markdown(run.get("query"))}',
             f'摘要：{_markdown(diagnosis.get("summary"))}', f'根因：{_markdown(diagnosis.get("root_cause"))}',
             f'置信度：{_markdown(diagnosis.get("confidence"))}',
             '引用：' + '、'.join(_markdown(value) for value in _list(diagnosis.get('evidence_ids'))[:30]),
             '', '## 验证与审批', '',
             '结论：' + ('本次回归修复已验证。' if verification.get('verified') is True else '尚未证实回归修复。'),
             f'执行器：{_markdown(verification.get("runner"))}',
             f'审批：{_markdown(approval.get("decision"))}；时间：{_markdown(approval.get("approved_at"))}',
             f'批准的补丁摘要：{_markdown(approval.get("patch_digest"))}', '',
             '| 阶段 | 退出码 | 耗时 ms | 来源 | 完成 | 执行 | 通过 | 失败 | 跳过 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name, label in [('baseline', '修改前'), ('patched', '修改后')]:
        phase = _dict(verification.get(name))
        tests = _dict(phase.get('tests'))
        values = [label, phase.get('exit_code'), phase.get('duration_ms'), tests.get('source'),
                  '是' if tests.get('completed') is True else '否',
                  *[tests.get(field) for field in ('executed', 'passed', 'failed', 'skipped')]]
        lines.append('| ' + ' | '.join(_markdown(value) for value in values) + ' |')
    lines.extend(['', '## 证据目录', '', '只含元数据，不含证据正文。', ''])
    for item in _list(report.get('evidence'))[:LIMITS['evidence']]:
        item = _dict(item)
        lines.append(f'- {_markdown(item.get("id"))}：{_markdown(item.get("path"))} '
                     f'L{_markdown(item.get("line_start"))}–L{_markdown(item.get("line_end"))}；'
                     f'来源 {_markdown(item.get("source"))}；类型 {_markdown(item.get("kind"))}；'
                     f'SHA256 {_markdown(item.get("sha256"))}')
    citations = _dict(report.get('citations'))
    if citations.get('complete') is not True:
        lines.extend(['', '未包含或不存在的引用：' + '、'.join(_markdown(value) for value in
                      _list(citations.get('unavailable_evidence_ids'))[:330])])
    lines.extend(['', '## 补丁摘要', '', '省略源码与原始 diff；前后片段仅保留 SHA256。', ''])
    for item in _list(report.get('patches'))[:LIMITS['patches']]:
        item = _dict(item)
        lines.extend([f'- 路径：{_markdown(item.get("path"))}；原因：{_markdown(item.get("reason"))}',
                      f'  修改前摘要：{_markdown(item.get("before_sha256"))}',
                      f'  修改后摘要：{_markdown(item.get("after_sha256"))}',
                      '  引用：' + '、'.join(_markdown(value) for value in _list(item.get('evidence_ids'))[:30])])
    lines.extend(['', '## 用量与审计', '',
                  (f'调用尝试：{_markdown(usage.get("provider_calls"))}；'
                   f'已记录输入 token：{_markdown(usage.get("input_tokens"))}；'
                   f'已记录输出 token：{_markdown(usage.get("output_tokens"))}'),
                  '事件仅含 ID、时间和类型，不含 payload。', ''])
    for item in _list(report.get('events'))[:LIMITS['events']]:
        item = _dict(item)
        lines.append(f'- {_markdown(item.get("id"))} · {_markdown(item.get("created_at"))} · {_markdown(item.get("kind"))}')
    lines.extend(['', '## 限制与完整性', ''])
    for scope in (diagnosis, verification, report):
        lines.extend('- ' + _markdown(value) for value in _list(scope.get('limitations'))[:40])
    for name, label in [('events', '事件'), ('evidence', '证据'), ('patches', '补丁')]:
        if report.get(name + '_truncated') is True:
            lines.append(f'- {label}列表已截断；请结合原始运行记录查看。')
    lines.extend(['', f'报告内容 SHA256：{_markdown(report.get("content_sha256"))}',
                  '摘要基于不含 content_sha256 字段的规范 JSON；不能用于校验 Markdown 文件本身，不是签名或外部证明。', ''])
    return '\n'.join(lines)
