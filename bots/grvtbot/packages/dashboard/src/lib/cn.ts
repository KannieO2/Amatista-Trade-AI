import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

// Standard cn() helper from shadcn — clsx for conditionals + tailwind-merge
// to dedupe conflicting Tailwind utility classes (e.g. "px-2 px-4" → "px-4").
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
