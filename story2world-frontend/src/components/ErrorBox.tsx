import { WarningIcon } from "./Icons";

export function ErrorBox({ message }: { message?: string | null }) {
  if (!message) return null;
  return (
    <div className="error-box" role="alert">
      <WarningIcon />
      <span>{message}</span>
    </div>
  );
}
