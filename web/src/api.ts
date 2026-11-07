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
