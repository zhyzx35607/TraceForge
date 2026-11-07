import { useEffect, useState } from 'react'
import { ArrowDown, ArrowUpRight, CheckCheck, ChevronDown, Clock3, Cpu, FileSearch, GitCompareArrows, LoaderCircle, Minus, ShieldCheck, XCircle } from 'lucide-react'
import { api } from './api'
import { isVerified } from './verification'
import type { Project, Run } from './types'

const statuses: Record<string, string> = { queued: '排队中', investigating: '调查中', awaiting_approval: '待审批', verifying: '验证中', completed: '已完成', failed: '失败', rejected: '已拒绝', cancelled: '已取消' }
const modes = { replay: '离线回放', live: '模型调查', investigate: '证据检索' }
const formatNumber = (value: unknown) => typeof value === 'number' ? value.toLocaleString('zh-CN') : '—'
const duration = (run?: Run) => {
  if (!run) return '—'
  const elapsed = Date.parse(run.updated_at) - Date.parse(run.created_at)
  if (!Number.isFinite(elapsed)) return '—'
  return elapsed >= 60000 ? `${(elapsed / 60000).toFixed(1)} 分钟` : `${(elapsed / 1000).toFixed(1)} 秒`
}
const rows: { label: string; sublabel?: string; get: (run: Run) => string }[] = [
  { label: '调查模式', get: run => modes[run.mode] },
  { label: '运行状态', get: run => statuses[run.status] || run.status },
  { label: '输入 Tokens', get: run => formatNumber(run.state.usage?.input_tokens) },
  { label: '输出 Tokens', get: run => formatNumber(run.state.usage?.output_tokens) },
  { label: '模型调用', get: run => formatNumber(run.state.usage?.provider_calls) },
  { label: '创建至最后更新', sublabel: '包含等待审批时间', get: run => duration(run) },
  { label: '测试执行耗时', get: run => run.state.verification ? `${(((run.state.verification.baseline?.duration_ms || 0) + (run.state.verification.patched?.duration_ms || 0)) / 1000).toFixed(2)} 秒` : '未执行' },
  { label: '收集证据', get: run => `${run.state.evidence?.length || 0} 条` },
  { label: '诊断引用', get: run => `${run.state.diagnosis?.evidence_ids?.length || 0} 条` },
  { label: '提出补丁', get: run => `${run.state.diagnosis?.patches?.length || 0} 处` },
  { label: '基线退出码', get: run => run.state.verification?.baseline ? String(run.state.verification.baseline.exit_code ?? '超时 / 未完成') : '未执行' },
  { label: '修复后退出码', get: run => run.state.verification?.patched ? String(run.state.verification.patched.exit_code ?? '超时 / 未完成') : '未执行' },
  { label: '基线实际测试', get: run => run.state.verification?.baseline?.tests ? `${run.state.verification.baseline.tests.executed} 项 · ${run.state.verification.baseline.tests.failed} 失败` : '未解析' },
  { label: '修复后实际测试', get: run => run.state.verification?.patched?.tests ? `${run.state.verification.patched.tests.executed} 项 · ${run.state.verification.patched.tests.passed} 通过` : '未解析' },
]

export default function ComparisonPanel({ runs, projects, refreshTick, onOpen }: { runs: Run[]; projects: Project[]; refreshTick: number; onOpen: (id: string) => void }) {
  const [leftId, setLeftId] = useState(runs[0]?.id || '')
  const [rightId, setRightId] = useState(runs[1]?.id || '')
  const [left, setLeft] = useState<Run | null>(null)
  const [right, setRight] = useState<Run | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setLeftId(id => id && runs.some(run => run.id === id) ? id : runs[0]?.id || '')
    setRightId(id => id && runs.some(run => run.id === id) ? id : runs[1]?.id || '')
  }, [runs])
  useEffect(() => {
    let disposed = false
    if (!leftId || !rightId) return
    setLoading(true); setError('')
    Promise.all([api<Run>(`/runs/${encodeURIComponent(leftId)}`), api<Run>(`/runs/${encodeURIComponent(rightId)}`)])
      .then(([first, second]) => { if (!disposed) { setLeft(first); setRight(second) } })
      .catch(err => { if (!disposed) setError(err instanceof Error ? err.message : String(err)) })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [leftId, rightId, refreshTick])
  if (runs.length < 2) return <section className="content-panel"><div className="empty-state"><div className="empty-icon"><GitCompareArrows size={31}/></div><h3>准备两条调查，开始比较</h3><p>选择两次真实运行，并排查看模型用量、检索证据、修复方案与验证结果。当前仅有 {runs.length} 条调查。</p></div></section>
  return <section className="content-panel comparison-panel"><div className="comparison-heading"><div><GitCompareArrows size={18}/><h3>比较真实调查结果</h3></div><span className="small-tag">RUN COMPARISON</span></div><div className="comparison-selectors"><div className="comparison-axis"><span>COMPARE</span><ArrowDown size={16}/></div>{[{ id: leftId, change: setLeftId, label: '调查 A', other: rightId }, { id: rightId, change: setRightId, label: '调查 B', other: leftId }].map(item => <label key={item.label} className="comparison-selector"><span>{item.label}</span><select value={item.id} aria-label={`选择${item.label}`} onChange={event => item.change(event.target.value)}>{runs.map(run => <option key={run.id} disabled={run.id === item.other} value={run.id}>{run.title} · #{run.id.slice(0, 8)}</option>)}</select><ChevronDown size={13}/></label>)}</div>{error && <div className="inline-error comparison-error"><XCircle size={16}/>{error}</div>}{loading ? <div className="loading-state"><LoaderCircle className="spin" size={20}/>读取两次调查的持久化结果…</div> : left && right ? <><div className="comparison-overview"><div className="comparison-axis"><ShieldCheck size={18}/><span>VERIFICATION</span></div>{[left, right].map((run, index) => <div className="comparison-run-card" key={`${run.id}-${index}`}><div className="comparison-project">{projects.find(project => project.id === run.project_id)?.name || run.project_id}</div><h3>{run.title}</h3><span className={`comparison-verdict ${isVerified(run.state.verification) ? 'comparison-verified' : ''}`}>{isVerified(run.state.verification) ? <CheckCheck size={15}/> : <Minus size={15}/>}{isVerified(run.state.verification) ? '回归修复已验证' : run.state.verification ? '尚未证明修复成功' : '尚未执行验证'}</span><button className="link-button" onClick={() => onOpen(run.id)}>查看完整调查 #{run.id.slice(0, 8)}<ArrowUpRight size={12}/></button></div>)}</div><div className="comparison-section-label"><Cpu size={13}/>执行数据与实际用量</div><div className="comparison-table">{rows.map(row => { const a = row.get(left); const b = row.get(right); return <div className={`comparison-row ${a !== b ? 'comparison-different' : ''}`} key={row.label}><div><strong>{row.label}</strong>{row.sublabel && <small>{row.sublabel}</small>}</div><span>{a}</span><span>{b}</span></div> })}</div><div className="comparison-section-label"><FileSearch size={13}/>诊断与证据</div><div className="comparison-diagnoses">{[left, right].map((run, index) => <article key={`${run.id}-${index}`}><span className="eyebrow">INVESTIGATION {index === 0 ? 'A' : 'B'}</span><h3>{run.state.diagnosis?.summary || '尚未生成诊断'}</h3><p>{run.state.diagnosis?.root_cause || '等待运行生成证据与诊断记录。'}</p><div className="comparison-evidence-list">{run.state.diagnosis?.evidence_ids?.map(id => { const evidence = run.state.evidence?.find(item => item.id === id); return <div key={id}><FileSearch size={12}/><span>{evidence ? `${evidence.path}:${evidence.line_start}–${evidence.line_end}` : id}</span></div> })}</div>{run.state.diagnosis?.limitations?.length ? <div className="comparison-limits">{run.state.diagnosis.limitations.map((limit, i) => <p key={i}>{limit}</p>)}</div> : null}</article>)}</div><div className="comparison-note"><Clock3 size={13}/>对比使用读取时的真实记录。运行进行中时，点击页面刷新获取最新结果；模式与问题不同会影响可比性。</div></> : null}</section>
}
