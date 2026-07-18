/* eslint-disable react-refresh/only-export-components */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function IconBase({ children, ...props }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
      {children}
    </svg>
  );
}

export const Icons = {
  book: (props: IconProps) => <IconBase {...props}><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5z"/><path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5z"/></IconBase>,
  archive: (props: IconProps) => <IconBase {...props}><path d="M4 7h16v13H4z"/><path d="M3 4h18v3H3zM9 11h6"/></IconBase>,
  task: (props: IconProps) => <IconBase {...props}><path d="M8 5h12M8 12h12M8 19h12"/><path d="m3 5 1 1 2-2m-3 8 1 1 2-2m-3 8 1 1 2-2"/></IconBase>,
  settings: (props: IconProps) => <IconBase {...props}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21h-4v-.09A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3v-4h.09A1.7 1.7 0 0 0 4.6 8.6a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3h4v.09A1.7 1.7 0 0 0 15.4 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.35.25.6.6.6 1v.4h1v4h-.09A1.7 1.7 0 0 0 19.4 15Z"/></IconBase>,
  upload: (props: IconProps) => <IconBase {...props}><path d="M12 16V4m-4 4 4-4 4 4M4 15v5h16v-5"/></IconBase>,
  search: (props: IconProps) => <IconBase {...props}><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></IconBase>,
  chevron: (props: IconProps) => <IconBase {...props}><path d="m9 5 7 7-7 7"/></IconBase>,
  back: (props: IconProps) => <IconBase {...props}><path d="m15 18-6-6 6-6"/></IconBase>,
  lock: (props: IconProps) => <IconBase {...props}><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></IconBase>,
  unlock: (props: IconProps) => <IconBase {...props}><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 7.5-2"/></IconBase>,
  panel: (props: IconProps) => <IconBase {...props}><rect x="3" y="4" width="18" height="16" rx="1"/><path d="M9 4v16"/></IconBase>,
  warning: (props: IconProps) => <IconBase {...props}><path d="m12 3 10 18H2z"/><path d="M12 9v5m0 3v.01"/></IconBase>,
  check: (props: IconProps) => <IconBase {...props}><path d="m5 12 4 4L19 6"/></IconBase>,
  refresh: (props: IconProps) => <IconBase {...props}><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 8A7 7 0 0 1 18.5 6L20 8M4 16l1.5 2A7 7 0 0 0 17.9 16"/></IconBase>,
  download: (props: IconProps) => <IconBase {...props}><path d="M12 3v12m-4-4 4 4 4-4M4 19h16"/></IconBase>,
  more: (props: IconProps) => <IconBase {...props}><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/></IconBase>,
};
