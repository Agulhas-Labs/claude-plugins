import type { Register } from 'claude-code'

import { register as band } from './band'

// A plugin names one hooks module; the band, the pane and the toast are one mod, registered here.
export const register: Register = (on, options) => {
  band(on, options)
}
