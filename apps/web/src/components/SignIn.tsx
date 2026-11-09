import { useEffect, useState } from 'react'
import { useLocation } from 'react-router'
import { LogIn } from 'lucide-react'
import { getAuthConfig, loginUrl } from '../api'
import ErrorAlert from '@/components/ErrorAlert'
import Logo from '@/components/Logo'
import { Button } from '@/components/ui/button'

// Shown when the API answers 401: with OIDC set (R4) people sign in; without it the web UI's
// shared key is missing or wrong.
export default function SignIn() {
  const { pathname, search } = useLocation()
  // undefined while asking, null when the API cannot be reached
  const [login, setLogin] = useState<boolean | null | undefined>(undefined)
  const params = new URLSearchParams(search)
  const failed = params.get('login_error')
  params.delete('login_error')
  const back = pathname + (params.size ? `?${params}` : '')

  useEffect(() => {
    getAuthConfig()
      .then((c) => setLogin(c.login))
      .catch(() => setLogin(null))
  }, [])

  return (
    <main className="flex min-h-svh items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm rounded-md border p-6">
        <p className="m-0 mb-4 flex items-center gap-2 font-semibold">
          <Logo /> enigram
        </p>
        {failed && <ErrorAlert>{failed}</ErrorAlert>}
        {login === true && (
          <>
            <h1 className="mb-2">Sign in</h1>
            <p className="mb-4 text-muted-foreground">
              Use your work account. The first time, you can read and ask; an admin gives you more.
            </p>
            <Button asChild className="w-full hover:no-underline">
              <a href={loginUrl(back)}>
                <LogIn aria-hidden /> Sign in
              </a>
            </Button>
          </>
        )}
        {login === false && (
          <p className="m-0 text-muted-foreground">
            The API refused the web UI's key. Check <code>ENIGRAM_WEB_KEY</code>, or set up sign-in
            with <code>OIDC_ISSUER</code> (see the README).
          </p>
        )}
        {login === null && <p className="m-0 text-muted-foreground">The API is unreachable.</p>}
      </div>
    </main>
  )
}
