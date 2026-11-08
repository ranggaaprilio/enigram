import { useCallback, useEffect, useState } from 'react'

// Light, dark, or the system's (default); kept per browser. index.html applies it before the
// first paint with the same key and rule.
export type Theme = 'light' | 'dark' | 'system'

const KEY = 'enigram-theme'
const DARK = '(prefers-color-scheme: dark)'

function stored(): Theme {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

function apply(theme: Theme) {
  const dark = theme === 'dark' || (theme === 'system' && window.matchMedia(DARK).matches)
  document.documentElement.classList.toggle('dark', dark)
}

export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setThemeState] = useState<Theme>(stored)

  useEffect(() => {
    apply(theme)
    if (theme !== 'system') return
    const media = window.matchMedia(DARK)
    const follow = () => apply('system')
    media.addEventListener('change', follow)
    return () => media.removeEventListener('change', follow)
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    try {
      if (next === 'system') localStorage.removeItem(KEY)
      else localStorage.setItem(KEY, next)
    } catch {
      // storage blocked: the choice lasts until reload
    }
    setThemeState(next)
  }, [])

  return [theme, setTheme]
}
