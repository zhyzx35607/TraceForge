export const TOKEN_KEY = 'traceforge-api-token'

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 15000)
  const token = sessionStorage.getItem(TOKEN_KEY)
  try {
    const response = await fetch(`/api${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
    })
    const data = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = data?.detail
      const message = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `请求失败（HTTP ${response.status}）`
      const failure = new Error(response.status === 401 ? `访问凭证无效或缺失。请在连接设置中填写 API Token。${message}` : message) as Error & { status?: number }
      failure.status = response.status
      throw failure
    }
    return data as T
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw new Error('请求超时，请检查后端服务是否正在运行。')
    if (error instanceof TypeError) throw new Error('无法连接后端服务。请确认 TraceForge 服务已在 127.0.0.1:8710 启动。')
    throw error
  } finally { window.clearTimeout(timeout) }
}

export async function downloadArtifact(id: string) {
  const token = sessionStorage.getItem(TOKEN_KEY)
  const response = await fetch(`/api/runs/${encodeURIComponent(id)}/artifact`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new Error(typeof data.detail === 'string' ? data.detail : '下载补丁失败')
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url; link.download = `traceforge-${id}.patch`; link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export type ReportFormat = 'markdown' | 'json'

export async function downloadReport(id: string, format: ReportFormat, signal: AbortSignal) {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) controller.abort()
  const timeout = window.setTimeout(abort, 15000)
  try {
    const token = sessionStorage.getItem(TOKEN_KEY)
    const response = await fetch(`/api/runs/${encodeURIComponent(id)}/report?format=${format}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: controller.signal,
    })
    if (!response.ok) {
      const data = await response.json().catch(() => null)
      const detail = typeof data?.detail === 'string' ? data.detail : `报告下载失败（HTTP ${response.status}）`
      throw new Error(response.status === 401 ? '访问凭证无效或缺失，请在连接设置中填写 API Token。' : detail)
    }
    const contentType = response.headers.get('Content-Type')?.split(';')[0].trim()
    if (contentType !== (format === 'json' ? 'application/json' : 'text/markdown')) {
      throw new Error('服务没有返回预期格式的报告，请确认后端已更新并重试。')
    }
    const blob = await response.blob()
    if (controller.signal.aborted) throw new DOMException('Download aborted', 'AbortError')
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    try {
      link.href = url
      link.download = `traceforge-${id}-report.${format === 'json' ? 'json' : 'md'}`
      document.body.append(link)
      link.click()
    } finally {
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    }
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError' && !signal.aborted) {
      throw new Error('报告下载超时，请检查后端服务并重试。')
    }
    if (error instanceof TypeError) throw new Error('无法下载报告，请检查后端连接并重试。')
    throw error
  } finally {
    window.clearTimeout(timeout)
    signal.removeEventListener('abort', abort)
  }
}
