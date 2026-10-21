import type { ReactNode } from "react";
import { DialogContent } from "./ui/overlay";

export function TaskPreviewFrame({ embedded, title, children, footer, className }: {
  embedded?: boolean;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
}) {
  if (!embedded) return <DialogContent title={title} footer={footer} className={className}>{children}</DialogContent>;
  return <section aria-label={title} className="space-y-3 rounded border border-line p-3">
    <h4 className="font-semibold">{title}</h4>
    {children}
    {footer && <div className="flex flex-wrap gap-2 border-t border-line pt-3">{footer}</div>}
  </section>;
}
