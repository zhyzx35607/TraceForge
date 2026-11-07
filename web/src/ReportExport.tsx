import { useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, Check, FileText, LoaderCircle, XCircle } from 'lucide-react'
import { downloadReport } from './api'
import type { ReportFormat } from './api'
import './report-export.css'

export default function ReportExport({ runId }: { runId: string }) {
  const request = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState<ReportFormat | null>(null)
  const [error, setError] = useState('')
  const [downloaded, setDownloaded] = useState<ReportFormat | null>(null)

  useEffect(() => () => request.current?.abort(), [])

  async function download(format: ReportFormat) {
    if (request.current) return
    const controller = new AbortController()
    request.current = controller
    setBusy(format)
    setError('')
    setDownloaded(null)
    try {
      await downloadReport(runId, format, controller.signal)
      if (!controller.signal.aborted) setDownloaded(format)
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '报告下载失败，请重试。')
    } finally {
      if (request.current === controller) request.current = null
      if (!controller.signal.aborted) setBusy(null)
    }
  }

  return <section className="report-export" aria-label="导出调查报告">
    <div className="report-export-row">
      <div className="report-export-copy"><FileText size={16}/><div><h3>导出调查报告</h3><p>报告包含诊断文字与证据索引</p></div></div>
      <div className="report-export-actions">
        {(['markdown', 'json'] as const).map(format => <button
          className="button secondary small" key={format} disabled={busy !== null}
          aria-label={`下载 ${format === 'markdown' ? 'Markdown' : 'JSON'} 报告`}
          onClick={() => void download(format)}
        >{busy === format ? <LoaderCircle className="spin" size={13}/> : <ArrowDownToLine size={13}/>}
          {format === 'markdown' ? 'Markdown' : 'JSON'}
        </button>)}
      </div>
    </div>
    {error && <div className="report-export-error" role="alert"><XCircle size={13}/><span>{error}</span></div>}
    <div className="report-export-status" role="status" aria-live="polite">
      {busy ? '正在准备报告…' : downloaded && <><Check size={12}/>{downloaded === 'markdown' ? 'Markdown' : 'JSON'} 报告已发起下载</>}
    </div>
  </section>
}
