import { apiFetch } from "./client";
import type { Order, RefundRequest } from "../types/domain";

export function fetchOrders(): Promise<Order[]> {
  return apiFetch<Order[]>("/api/orders/");
}

export function fetchOrderDetail(orderId: string): Promise<Order> {
  return apiFetch<Order>(`/api/orders/${orderId}/`);
}

export function fetchOrderRefunds(orderId: string): Promise<RefundRequest[]> {
  return apiFetch<RefundRequest[]>(`/api/orders/${orderId}/refunds/`);
}

export function fetchMyRefunds(): Promise<RefundRequest[]> {
  return apiFetch<RefundRequest[]>("/api/refunds/");
}
