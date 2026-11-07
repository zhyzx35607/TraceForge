import { Check, ChevronDown, Clock3, FileSearch, Fingerprint, Search, ShieldCheck } from 'lucide-react'
import './retrieval-evaluation.css'

const methods = ['alphabetical', 'bm25', 'char', 'rrf'] as const
type Method = typeof methods[number]
type Ratio = { numerator?: number; denominator?: number; rate?: number }
type Scores = { recall_at_k?: Record<string, number>; reciprocal_rank?: number; all_required_at_k?: Record<string, boolean> }
type MethodSummary = { query_count?: number; macro_recall_at_k?: Record<string, number>; mrr?: number; all_required_at_k?: Record<string, Ratio>; timings_ms?: { p50?: number; p95?: number } }
type MethodResult = { ranking?: string[]; metrics?: Scores; query_ms?: number }
type Difference = { recall_at_k_delta?: Record<string, number>; reciprocal_rank_delta?: number; relevant_rank_changes?: { path: string; bm25_rank?: number | null; method_rank?: number | null }[] }
type RetrievalCase = { case_id: string; corpus_prefix?: string; query?: string; relevant_paths?: string[]; methods?: Partial<Record<Method, MethodResult>>; versus_bm25?: Partial<Record<Method, Difference>> }
type RetrievalReport = {
  generated_at?: string; provider_calls?: number; queries_sha256?: string; qrels_sha256?: string
  corpus?: { files?: number; chunks?: number; bytes?: number; sha256?: string }
  configuration?: { candidate_limit_per_lexical_route?: number; returned_evidence_limit?: number; query_character_limit?: number; chunk_lines?: number; chunk_characters?: number; long_line_overlap?: number; rrf?: { k?: number } }
  timings?: { cold_build_ms?: number; total_ms?: number }
  methods?: Partial<Record<Method, MethodSummary>>; cases?: RetrievalCase[]; limitations?: string[]
  [key: string]: unknown
}
const labels: Record<Method, { name: string; detail: string }> = {
  alphabetical: { name: '字母序基线', detail: '按完整文件路径排序' },
  bm25: { name: 'BM25', detail: '词项匹配与长度归一化' },
  char: { name: '字符 TF-IDF', detail: '字符 n-gram 余弦相似度' },
  rrf: { name: 'RRF 融合', detail: '合并两路候选的排名' },
}
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const integer = (value?: number) => finite(value) ? value.toLocaleString('zh-CN') : '—'
const percent = (value?: number) => finite(value) ? `${(value * 100).toFixed(1)}%` : '—'
const decimal = (value?: number) => finite(value) ? value.toFixed(3) : '—'
const duration = (value?: number) => finite(value) ? `${value.toFixed(2)} ms` : '—'
const rank = (value?: number | null) => finite(value) ? `#${value}` : '未返回'
const deltaClass = (value?: number) => finite(value) ? value > 0 ? 'delta-positive' : value < 0 ? 'delta-negative' : '' : ''
const delta = (value?: number, percentage = true) => finite(value) ? `${value > 0 ? '+' : ''}${(percentage ? value * 100 : value).toFixed(percentage ? 1 : 3)}${percentage ? ' pp' : ''}` : '—'

function RankingCard({ method, result, difference, relevant }: { method: Method; result?: MethodResult; difference?: Difference; relevant: Set<string> }) {
  const ranking = Array.isArray(result?.ranking) ? result.ranking.slice(0, 8) : []
  return <article className="retrieval-ranking">
    <h4>{labels[method].name}</h4>
    <div className="retrieval-ranking-metrics"><span>Recall@8 <b>{percent(result?.metrics?.recall_at_k?.['8'])}</b></span><span>RR <b>{decimal(result?.metrics?.reciprocal_rank)}</b></span></div>
    {ranking.length ? <ol className="retrieval-ranking-list">{ranking.map((path, index) => <li className={relevant.has(path) ? 'is-relevant' : ''} key={path}><span>{index + 1}</span><code>{path}</code>{relevant.has(path) && <em><Check size={10}/>相关</em>}</li>)}</ol> : <p className="retrieval-note">没有返回匹配路径。</p>}
    {method === 'bm25' ? <p className="retrieval-note">以下差异以 BM25 为比较基准。</p> : <>
      <div className="retrieval-deltas"><span className="retrieval-delta-label">相对 BM25</span>{['3', '5', '8'].map(k => <span key={k}>ΔRecall@{k} <b className={deltaClass(difference?.recall_at_k_delta?.[k])}>{delta(difference?.recall_at_k_delta?.[k])}</b></span>)}<span>ΔRR <b className={deltaClass(difference?.reciprocal_rank_delta)}>{delta(difference?.reciprocal_rank_delta, false)}</b></span></div>
      {!!difference?.relevant_rank_changes?.length && <details className="retrieval-rank-changes"><summary>相关文件排名变化<ChevronDown size={12}/></summary>{difference.relevant_rank_changes.map(change => <div key={change.path}><code>{change.path}</code><span>BM25 {rank(change.bm25_rank)} → {rank(change.method_rank)}</span></div>)}</details>}
    </>}
  </article>
}

export default function RetrievalEvaluation({ report }: { report: RetrievalReport }) {
  const cases = Array.isArray(report.cases) ? report.cases : []
  const configuration = report.configuration || {}
  const generated = report.generated_at ? new Date(report.generated_at) : null
  return <div className="retrieval-evaluation">
    <section className="retrieval-hero"><div className="retrieval-hero-title"><FileSearch size={24}/><div><span className="eyebrow">SYNTHETIC PILOT</span><h3>把每种检索方法放在同一份语料里比较</h3></div><span className="small-tag">{report.provider_calls === 0 ? 'NO MODEL CALLS' : `模型调用 ${integer(report.provider_calls)}`}</span></div><p>这是公开合成案例上的词法检索试验。所有结果来自已保存报告，用于审阅前排准确性与后段覆盖之间的取舍；不能据此推断模型诊断、修复或生产环境的效果。</p></section>
    <div className="retrieval-meta"><span><Clock3 size={12}/>{generated && !Number.isNaN(generated.valueOf()) ? generated.toLocaleString('zh-CN') : '未记录生成时间'}</span><span>冷建索引 {duration(report.timings?.cold_build_ms)}</span><span>路径去重后评测</span></div>
    <div className="retrieval-counts"><div><span>问题数</span><strong>{integer(cases.length)}</strong></div><div><span>公开文件</span><strong>{integer(report.corpus?.files)}</strong></div><div><span>索引片段</span><strong>{integer(report.corpus?.chunks)}</strong></div><div><span>模型调用</span><strong>{integer(report.provider_calls)}</strong></div></div>
    <div className="retrieval-table-scroll" role="region" aria-label="检索方法指标表，可横向滚动" tabIndex={0}><table className="retrieval-method-table"><caption>四种方法的文件级检索结果</caption><thead><tr><th scope="col">方法</th><th scope="col">Recall@3</th><th scope="col">Recall@5</th><th scope="col">Recall@8</th><th scope="col">MRR</th><th scope="col">全部相关文件 @8</th></tr></thead><tbody>{methods.map(method => {
      const summary = report.methods?.[method]
      const all = summary?.all_required_at_k?.['8']
      return <tr key={method}><th scope="row"><strong>{labels[method].name}</strong><small>{labels[method].detail}</small></th>{['3', '5', '8'].map(k => <td key={k}>{percent(summary?.macro_recall_at_k?.[k])}</td>)}<td>{decimal(summary?.mrr)}</td><td><strong>{percent(all?.rate)}</strong><small>{integer(all?.numerator)} / {integer(all?.denominator)} 个问题</small></td></tr>
    })}</tbody></table></div>
    <p className="retrieval-note">各项指标独立比较，没有综合胜者评分。下方逐案例排名保留每种方法的提升与退步。</p>
    <details className="retrieval-definitions" open><summary><ShieldCheck size={14}/>指标定义与固定边界<ChevronDown size={13}/></summary><dl><div><dt>Recall@k · 宏平均</dt><dd>每个问题先计算前 k 个去重路径中命中的相关文件数 ÷ 本问题全部相关文件数，再对所有问题取平均。</dd></div><div><dt>MRR · 平均倒数排名</dt><dd>取每个问题第一个相关文件排名的倒数，再取平均；返回列表中没有命中则记为 0。逐案例的 RR 是单个问题的倒数排名。</dd></div><div><dt>全部相关文件 @8</dt><dd>前 8 个路径覆盖本问题全部相关文件的问题数 ÷ 所有问题数。包含应拒答案例，缺失结果也计入分母。</dd></div><div><dt>可比较范围</dt><dd>三个词法方法共用生产检索的分块、上下文展开和证据去重；字母序基线只按路径排序。问题文本、相关性标注和 hidden / oracle 材料不进入语料。</dd></div></dl><div className="retrieval-limits"><span>每路候选上限 <b>{integer(configuration.candidate_limit_per_lexical_route)}</b></span><span>返回证据上限 <b>{integer(configuration.returned_evidence_limit)}</b></span><span>问题字符上限 <b>{integer(configuration.query_character_limit)}</b></span><span>分块 <b>{integer(configuration.chunk_lines)} 行 / {integer(configuration.chunk_characters)} 字符</b></span><span>长行重叠 <b>{integer(configuration.long_line_overlap)} 字符</b></span><span>RRF k <b>{integer(configuration.rrf?.k)}</b></span></div><p className="retrieval-note">MRR 受返回证据上限约束，按路径去重后可能少于此数量。当前结果来自小规模合成语料，没有独立留出集或置信区间。</p></details>
    <div className="retrieval-case-heading"><div><h3>逐问题检查 <span>{cases.length}</span></h3><p>展开查看外置问题、相关文件标注（qrels）、前 8 个路径及相对 BM25 的差异。</p></div><Search size={18}/></div>
    <div className="retrieval-cases">{cases.map(item => {
      const relevant = new Set(item.relevant_paths || [])
      return <details className="retrieval-case" key={item.case_id}><summary><div><strong>{item.case_id}</strong><small>{item.corpus_prefix || '—'} · {relevant.size} 个相关文件</small></div><span>四路排名</span><ChevronDown size={14}/></summary><div className="retrieval-case-body"><p className="retrieval-query">{item.query || '未记录问题文本。'}</p><div className="retrieval-qrels"><span>相关文件 · QRELS</span>{[...relevant].map(path => <code key={path}>{path}</code>)}</div><div className="retrieval-rankings">{methods.map(method => <RankingCard key={method} method={method} result={item.methods?.[method]} difference={item.versus_bm25?.[method]} relevant={relevant}/>)}</div><p className="retrieval-note">pp 表示百分点；正负号表示相对 BM25 的指标差值。文件排名越小越靠前，“未返回”表示不在此次保存的返回列表中。</p></div></details>
    })}</div>
    <details className="retrieval-provenance"><summary><Fingerprint size={14}/>数据指纹与耗时记录<ChevronDown size={13}/></summary><dl><div><dt>Corpus SHA256</dt><dd><code>{report.corpus?.sha256 || '—'}</code></dd></div><div><dt>Queries SHA256</dt><dd><code>{report.queries_sha256 || '—'}</code></dd></div><div><dt>Qrels SHA256</dt><dd><code>{report.qrels_sha256 || '—'}</code></dd></div><div><dt>语料字节 / 全程耗时</dt><dd>{integer(report.corpus?.bytes)} bytes · {duration(report.timings?.total_ms)}</dd></div>{methods.map(method => <div key={method}><dt>{labels[method].name} 查询 p50 / p95</dt><dd>{duration(report.methods?.[method]?.timings_ms?.p50)} / {duration(report.methods?.[method]?.timings_ms?.p95)}</dd></div>)}</dl><p className="retrieval-note">每个问题、每种方法只执行一轮；词法查询包含生产检索的磁盘刷新。文件缓存和固定执行顺序会影响耗时，不能将此处单次计时外推为稳定性能。</p></details>
    <details className="raw-report"><summary><span className="mono">{'{ }'}</span>查看完整原始报告<ChevronDown size={13}/></summary><pre className="json-report">{JSON.stringify(report, null, 2)}</pre></details>
  </div>
}
