import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { StatCard } from '../index';

describe('StatCard', () => {
  it('renders label and value correctly', () => {
    const { getByText } = render(
      <StatCard label="Test Label" value="₹1,234" />
    );
    
    expect(getByText('Test Label')).toBeDefined();
    expect(getByText('₹1,234')).toBeDefined();
  });

  it('renders sub-text if provided', () => {
    const { getByText } = render(
      <StatCard label="Test" value="100" sub="10 trades" />
    );
    
    expect(getByText('10 trades')).toBeDefined();
  });

  it('applies accent classes correctly', () => {
    const { container } = render(
      <StatCard label="P&L" value="+5%" accent="up" />
    );
    
    // Check for "up" related classes in the card div
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('border-up/30');
    expect(card.className).toContain('bg-up/5');
  });
});
