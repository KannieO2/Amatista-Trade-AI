import { Navigate } from 'react-router-dom';
import { useAuth } from '@/lib/auth-context';
import { Button } from '@/components/primitives/button';
import { useT } from '@/i18n';

interface Props {
  children: React.ReactNode;
  requireGrvt?: boolean;
}

// El grid no tiene login propio: la sesión sale de Amatista, que siembra el
// token en el HTML antes de que arranque el bundle. Sin token no hay a dónde
// mandar al usuario — el formulario de login se eliminó y el proxy responde 403
// a /auth/login. Así que explicamos qué pasó y ofrecemos recargar, que es lo
// que vuelve a sembrar el token.
function SessionRequired() {
  const t = useT();
  return (
    <div className="min-h-dvh flex items-center justify-center p-4 bg-bg-base">
      <div className="w-full max-w-sm text-center space-y-4">
        <h1 className="text-lg font-semibold text-text-primary">
          {t('auth.sessionRequired.title')}
        </h1>
        <p className="text-sm text-text-muted">
          {t('auth.sessionRequired.body')}
        </p>
        <Button
          variant="primary"
          className="w-full"
          onClick={() => window.location.reload()}
        >
          {t('auth.sessionRequired.reload')}
        </Button>
      </div>
    </div>
  );
}

export function ProtectedRoute({ children, requireGrvt = true }: Props) {
  const { user, token, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-sm text-text-muted animate-pulse">
        Loading...
      </div>
    );
  }

  if (!token) return <SessionRequired />;
  if (!user) return <SessionRequired />;
  if (requireGrvt && !user.hasGrvtCreds) {
    return <Navigate to="/onboarding/grvt" replace />;
  }

  return <>{children}</>;
}
