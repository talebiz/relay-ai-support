/**
 * These types mirror the DRF serializers on the backend field-for-field
 * (accounts/serializers.py::MeSerializer, orders/serializers.py::*).
 * There is no code generation step wiring them together - if a backend
 * serializer field is renamed, this file must be updated by hand. That's
 * an accepted tradeoff for this project's size; a larger project would
 * reach for the "Automatic OpenAPI generation from DRF" bonus item
 * (project brief, section 5) and generate these instead.
 */

export type UserRole = "customer" | "support_agent";

export interface Me {
  id: number;
  username: string;
  email: string;
  role: UserRole;
}

export interface Product {
  id: number;
  name: string;
  sku: string;
  description: string;
  price_cents: number;
}

export type OrderStatus =
  | "pending"
  | "processing"
  | "shipped"
  | "delivered"
  | "cancelled";

export interface Order {
  id: string; // UUID
  username: string;
  product: Product;
  quantity: number;
  status: OrderStatus;
  tracking_number: string;
  total_cents: number;
  created_at: string; // ISO datetime
  updated_at: string;
}

export type RefundStatus = "requested" | "approved" | "denied" | "refunded";

export interface RefundRequest {
  id: string; // UUID
  order_id: string; // UUID
  reason: string;
  status: RefundStatus;
  resolution_note: string;
  created_at: string;
}

export interface TokenPair {
  access: string;
  refresh: string;
}
