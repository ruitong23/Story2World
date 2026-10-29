import type { SVGProps } from "react";

type Props = SVGProps<SVGSVGElement>;

function Icon({ children, ...props }: Props & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export const BookIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5z" />
    <path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5z" />
  </Icon>
);

export const UploadIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M12 16V4" />
    <path d="m7 9 5-5 5 5" />
    <path d="M5 20h14" />
  </Icon>
);

export const FolderIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M3 7.5h7l2-3h9v15H3z" />
  </Icon>
);

export const ArrowIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M5 12h14" />
    <path d="m14 7 5 5-5 5" />
  </Icon>
);

export const KeyIcon = (props: Props) => (
  <Icon {...props}>
    <circle cx="8" cy="15" r="4" />
    <path d="m11 12 8-8" />
    <path d="m16 7 2 2" />
  </Icon>
);

export const SparkIcon = (props: Props) => (
  <Icon {...props}>
    <path d="m12 3 1.2 4.1L17 9l-3.8 1.9L12 15l-1.2-4.1L7 9l3.8-1.9z" />
    <path d="m18.5 15 .6 2.1L21 18l-1.9.9-.6 2.1-.6-2.1L16 18l1.9-.9z" />
  </Icon>
);

export const UsersIcon = (props: Props) => (
  <Icon {...props}>
    <circle cx="9" cy="8" r="3" />
    <path d="M3.5 20a5.5 5.5 0 0 1 11 0" />
    <path d="M16 5.5a3 3 0 0 1 0 5.8" />
    <path d="M17 15a5 5 0 0 1 3.5 5" />
  </Icon>
);

export const GlobeIcon = (props: Props) => (
  <Icon {...props}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18" />
    <path d="M12 3a14 14 0 0 1 0 18" />
    <path d="M12 3a14 14 0 0 0 0 18" />
  </Icon>
);

export const ChatIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M4 5h16v11H9l-5 4z" />
    <path d="M8 9h8M8 12h5" />
  </Icon>
);

export const CheckIcon = (props: Props) => (
  <Icon {...props}>
    <path d="m5 12 4 4L19 6" />
  </Icon>
);

export const WarningIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M12 3 2.8 20h18.4z" />
    <path d="M12 9v5M12 17h.01" />
  </Icon>
);

export const HeartIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M20.8 5.8a5.4 5.4 0 0 0-7.7 0L12 7l-1.1-1.2a5.4 5.4 0 0 0-7.7 7.6L12 22l8.8-8.6a5.4 5.4 0 0 0 0-7.6z" />
  </Icon>
);

export const ShieldIcon = (props: Props) => (
  <Icon {...props}>
    <path d="M12 3 4.5 6v5.5c0 4.7 3 7.8 7.5 9.5 4.5-1.7 7.5-4.8 7.5-9.5V6z" />
    <path d="m9 12 2 2 4-5" />
  </Icon>
);

export const TargetIcon = (props: Props) => (
  <Icon {...props}>
    <circle cx="12" cy="12" r="8" />
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M22 12h-3M12 22v-3M2 12h3" />
  </Icon>
);
