import { HttpService } from '@nestjs/axios';
import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { firstValueFrom } from 'rxjs';

/**
 * Emite alertas de inventario hacia el chatbot (saliente ERP -> bot).
 *
 * Cuando una operación del ERP reduce el stock de un producto por debajo de su
 * umbral, llama a `maybeEmit(...)`: si quedó bajo umbral, hace POST a
 * `POST {BOT_WEBHOOK_URL}/webhooks/erp/inventory-alert` con el header
 * `X-Webhook-Secret: {BOT_WEBHOOK_SECRET}` (debe coincidir con `ERP_WEBHOOK_SECRET`
 * del chatbot).
 *
 * Requiere `HttpModule` (@nestjs/axios) importado en el módulo correspondiente.
 *
 * Variables de entorno del ERP:
 *   BOT_WEBHOOK_URL     p. ej. https://chatbot-intergranel.up.railway.app
 *   BOT_WEBHOOK_SECRET  mismo valor que ERP_WEBHOOK_SECRET en el chatbot
 */
@Injectable()
export class BotInventoryAlertEmitter {
  private readonly logger = new Logger(BotInventoryAlertEmitter.name);

  constructor(
    private readonly http: HttpService,
    private readonly config: ConfigService,
  ) {}

  /** Emite la alerta solo si el stock quedó por debajo del umbral. */
  async maybeEmit(item: {
    producto: string;
    stockTon: number;
    umbralTon: number;
    ubicacion?: string | null;
  }): Promise<void> {
    if (item.stockTon >= item.umbralTon) {
      return;
    }
    await this.emit(item);
  }

  async emit(item: {
    producto: string;
    stockTon: number;
    umbralTon: number;
    ubicacion?: string | null;
    mensaje?: string | null;
  }): Promise<void> {
    const baseUrl = this.config.get<string>('BOT_WEBHOOK_URL');
    const secret = this.config.get<string>('BOT_WEBHOOK_SECRET');
    if (!baseUrl) {
      this.logger.warn('BOT_WEBHOOK_URL no configurada; no se emite la alerta');
      return;
    }

    const url = `${baseUrl.replace(/\/$/, '')}/webhooks/erp/inventory-alert`;
    const payload = {
      producto: item.producto,
      stock_ton: item.stockTon,
      umbral_ton: item.umbralTon,
      ubicacion: item.ubicacion ?? null,
      mensaje: item.mensaje ?? null,
    };

    try {
      await firstValueFrom(
        this.http.post(url, payload, {
          headers: secret ? { 'X-Webhook-Secret': secret } : {},
          timeout: 10_000,
        }),
      );
      this.logger.log(`Alerta de inventario enviada al bot: ${item.producto}`);
    } catch (err) {
      // No interrumpas la operación del ERP por un fallo de notificación.
      this.logger.error(
        `No se pudo enviar la alerta de inventario (${item.producto})`,
        err as Error,
      );
    }
  }
}
