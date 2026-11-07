import type { Verification } from './types'

export function isVerified(verification?: Verification): boolean {
  if (!verification?.regression_fixed) return false
  if (verification.baseline?.tests?.executed === 0 || verification.patched?.tests?.executed === 0) return false
  return true
}

export function verificationLimitations(verification: Verification): string[] {
  const limitations = [...(verification.limitations || [])]
  if (verification.baseline?.tests?.executed === 0 || verification.patched?.tests?.executed === 0) limitations.unshift('至少一次执行没有实际运行测试；进程退出码为 0 不代表验证通过。')
  if (!verification.baseline?.tests || !verification.patched?.tests) limitations.push('此记录未提供完整的测试数量解析结果，请结合原始输出审阅执行覆盖范围。')
  return [...new Set(limitations)]
}
