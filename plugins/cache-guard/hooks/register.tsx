import type { Register } from 'claude-code'

import { register as band } from './band'
import { register as resume } from './resume'

// A plugin names one hooks module, so the status band and the resume offer register through this entry.
export const register: Register = (on, options) => {
  band(on, options)
  resume(on, options)
}
