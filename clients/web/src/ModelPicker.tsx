import { TIER_HINT } from "@enzo/chat";
import { type Brain, TIERS } from "@enzo/sdk";

/**
 * Smart routing, any model of a tier, or one model. Models whose provider is not connected are
 * listed but disabled, with the account they need, so the catalog stays visible (spec §1.3.6).
 */
export function ModelPicker({
  brains,
  value,
  disabled,
  onChange,
}: {
  brains: Brain[];
  value: string | null;
  disabled: boolean;
  onChange: (choice: string | null) => void;
}) {
  return (
    <select
      aria-label="Model"
      value={value ?? ""}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">Auto (smart routing)</option>
      <optgroup label="Any model of a tier">
        {TIERS.map((tier) => (
          <option
            key={tier}
            value={tier}
            disabled={!brains.some((b) => b.tier === tier && b.available)}
          >
            Any {tier} model
          </option>
        ))}
      </optgroup>
      {TIERS.map((tier) => {
        const inTier = brains.filter((b) => b.tier === tier);
        if (inTier.length === 0) return null;
        return (
          <optgroup key={tier} label={`${tier}: ${TIER_HINT[tier]}`}>
            {inTier.map((b) => (
              <option key={b.name} value={b.name} disabled={!b.available}>
                {b.recommended ? "★ " : ""}
                {b.name} · {b.model}
                {b.priced ? "" : " · cost unknown"}
                {b.available ? "" : ` · connect ${b.provider}`}
              </option>
            ))}
          </optgroup>
        );
      })}
    </select>
  );
}
