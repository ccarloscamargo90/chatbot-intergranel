import {
  Body,
  Controller,
  Get,
  NotFoundException,
  Param,
  Post,
  UseGuards,
} from '@nestjs/common';
import { ApiOperation, ApiSecurity, ApiTags } from '@nestjs/swagger';
import { IsNumber, IsPositive, IsString } from 'class-validator';
import { BotApiKeyGuard } from './bot-api-key.guard';
import {
  BotPriceDto,
  BotQuoteDto,
  BotRequestDto,
  BotVentasService,
} from './bot-ventas.service';
// Ajusta el import al decorador real de bypass del guard JWT global de tu repo.
import { Public } from '../../common/decorators/public.decorator';

class CrearCotizacionDto {
  @IsString() producto: string;
  @IsNumber() @IsPositive() cantidad: number;
  @IsString() telefono: string;
}

class CrearSolicitudDto {
  @IsString() producto: string;
  @IsNumber() @IsPositive() cantidad: number;
  @IsString() telefono: string;
}

@ApiTags('bot')
@ApiSecurity('bot-api-key')
@Public()
@UseGuards(BotApiKeyGuard)
@Controller({ path: 'bot', version: '1' })
export class BotVentasController {
  constructor(private readonly ventas: BotVentasService) {}

  @Get('precios/:producto')
  @ApiOperation({ summary: 'Precio vigente y disponibilidad de un producto' })
  async getPrice(@Param('producto') producto: string): Promise<BotPriceDto> {
    const price = await this.ventas.getPrice(producto);
    if (!price) {
      throw new NotFoundException(`Sin precio para ${producto}`);
    }
    return price;
  }

  @Get('precios')
  @ApiOperation({ summary: 'Lista de precios vigentes' })
  async listPrices(): Promise<BotPriceDto[]> {
    return this.ventas.listPrices();
  }

  @Post('cotizaciones')
  @ApiOperation({ summary: 'Genera una cotización (producto + cantidad)' })
  async createQuote(@Body() dto: CrearCotizacionDto): Promise<BotQuoteDto> {
    const quote = await this.ventas.createQuote(
      dto.producto,
      dto.cantidad,
      dto.telefono,
    );
    if (!quote) {
      throw new NotFoundException(`Sin precio para ${dto.producto}`);
    }
    return quote;
  }

  @Post('solicitudes')
  @ApiOperation({ summary: 'Registra una solicitud de pedido' })
  async createRequest(
    @Body() dto: CrearSolicitudDto,
  ): Promise<BotRequestDto> {
    return this.ventas.createRequest(dto.producto, dto.cantidad, dto.telefono);
  }
}
