export const chatAnswerModes = ['grounded_model', 'grounded_selection', 'computed', 'source_reference', 'insufficient'] as const

export type ChatAnswerMode = typeof chatAnswerModes[number]

export function isChatAnswerMode(value: unknown): value is ChatAnswerMode {
  return chatAnswerModes.some(mode => mode === value)
}
