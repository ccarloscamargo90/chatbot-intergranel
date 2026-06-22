import { Injectable } from '@nestjs/common';
import { PrismaService } from '../../prisma/prisma.service';

/** Precio vigente que consume el chatbot (modelo `Price` de `app/models.py`). */
export interface BotPriceDto {
  producto: string;
  precio_ton: number;
  moneda: string;
  disponible_ton: number | null;
  vigencia: string | null;
}

/** Cotización que consume el chatbot (modelo `Quote`). */
export interface BotQuoteDto {
  id: string;
  producto: string;
  cantidad: number;
  total: number;
  moneda: string;
  vigencia: string | null;
  estado: string;
}

/** Solicitud de pedido que consume el chatbot (modelo `PurchaseRequest`). */
export interface BotRequestDto {
  id: string;
  producto: string;
  cantidad: number;
  telefono: string | null;
  estado: string;
}

@Injectable()
export class BotVentasService {
  constructor(private readonly prisma: PrismaService) {}

  // Ajusta el nombre del modelo/campos de Prisma a tu esquema real. Aquí se
  // asume un modelo `Precio` con: producto, precioTon (BigInt en centavos),
  // moneda, disponibleTon (Decimal) y vigencia (DateTime).
  private toPriceDto(p: any): BotPriceDto {
    return {
      producto: p.producto,
      precio_ton: p.precioTon != null ? Number(p.precioTon) / 100 : 0,
      moneda: p.moneda ?? 'MXN',
      disponible_ton: p.disponibleTon != null ? Number(p.disponibleTon) : null,
      vigencia: p.vigencia?.toISOString().slice(0, 10) ?? null,
    };
  }

  /** Búsqueda flexible por nombre de producto (case-insensitive). */
  async getPrice(producto: string): Promise<BotPriceDto | null> {
    const p = await this.prisma.precio.findFirst({
      where: { producto: { equals: producto, mode: 'insensitive' } },
    });
    return p ? this.toPriceDto(p) : null;
  }

  async listPrices(): Promise<BotPriceDto[]> {
    const precios = await this.prisma.precio.findMany({
      orderBy: { producto: 'asc' },
    });
    return precios.map((p) => this.toPriceDto(p));
  }

  /**
   * Crea una cotización a partir del precio vigente. Devuelve null si el
   * producto no tiene precio (el chatbot lo interpreta como "no encontrado").
   */
  async createQuote(
    producto: string,
    cantidad: number,
    telefono: string,
  ): Promise<BotQuoteDto | null> {
    const precio = await this.prisma.precio.findFirst({
      where: { producto: { equals: producto, mode: 'insensitive' } },
    });
    if (!precio) {
      return null;
    }
    const precioTon = Number(precio.precioTon) / 100;
    const total = Math.round(precioTon * cantidad * 100) / 100;

    const cotizacion = await this.prisma.cotizacion.create({
      data: {
        producto: precio.producto,
        cantidad,
        precioTon: precio.precioTon,
        total: Math.round(total * 100), // centavos
        moneda: precio.moneda ?? 'MXN',
        telefono,
        estado: 'borrador',
      },
    });

    return {
      id: cotizacion.folio ?? `COT-${cotizacion.id}`,
      producto: cotizacion.producto,
      cantidad: Number(cotizacion.cantidad),
      total,
      moneda: cotizacion.moneda ?? 'MXN',
      vigencia: precio.vigencia?.toISOString().slice(0, 10) ?? null,
      estado: cotizacion.estado ?? 'borrador',
    };
  }

  /** Registra una solicitud de pedido (estado 'pendiente'). */
  async createRequest(
    producto: string,
    cantidad: number,
    telefono: string,
  ): Promise<BotRequestDto> {
    const solicitud = await this.prisma.solicitudPedido.create({
      data: { producto, cantidad, telefono, estado: 'pendiente' },
    });
    return {
      id: solicitud.folio ?? `SOL-${solicitud.id}`,
      producto: solicitud.producto,
      cantidad: Number(solicitud.cantidad),
      telefono: solicitud.telefono ?? null,
      estado: solicitud.estado ?? 'pendiente',
    };
  }
}
