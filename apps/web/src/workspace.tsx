import { Link, useParams, type LinkProps } from 'react-router'

// The API's rule (apps/api/src/enigram/domain/workspace.py): safe as a folder and a URL segment.
export const WORKSPACE_NAME = /^[a-z0-9][a-z0-9-]{0,31}$/
export const WORKSPACE_RULE =
  '1-32 lowercase letters, digits or "-", starting with a letter or digit'

// Turns an app path ("/trace/5") into the current workspace's ("/w/ws-a/trace/5"); unchanged
// outside a workspace route (the page tests render pages on their own).
export function useWorkspacePath() {
  const { workspace } = useParams()
  return (path: string) => (workspace ? `/w/${workspace}${path}` : path)
}

// A Link to an app path that stays in the current workspace.
export function WsLink({ to, ...props }: Omit<LinkProps, 'to'> & { to: string }) {
  return <Link to={useWorkspacePath()(to)} {...props} />
}
