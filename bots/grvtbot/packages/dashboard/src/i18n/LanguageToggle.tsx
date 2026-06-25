import { useLang } from './context';
import { twMerge } from 'tailwind-merge';

interface Props {
  variant?: 'default' | 'compact';
  className?: string;
}

export function LanguageToggle({ variant = 'default', className }: Props) {
  const { lang, setLang } = useLang();
  const isCompact = variant === 'compact';
  const sizeCls = isCompact
    ? 'px-2 py-0.5 text-2xs'
    : 'px-3 py-1.5 text-xs';

  const cell = (l: 'es' | 'en', label: string) => (
    <button
      key={l}
      type="button"
      onClick={() => setLang(l)}
      aria-pressed={lang === l}
      className={twMerge(
        'font-medium transition-colors',
        sizeCls,
        lang === l
          ? 'bg-primary text-white'
          : 'text-text-secondary hover:text-text-primary'
      )}
    >
      {label}
    </button>
  );

  return (
    <div
      role="group"
      aria-label="Language"
      className={twMerge(
        'inline-flex rounded-md border border-border-subtle overflow-hidden',
        className
      )}
    >
      {cell('es', 'ES')}
      {cell('en', 'EN')}
    </div>
  );
}
